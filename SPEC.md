# SPEC — Gemini SQL Assistant

## Purpose

Gemini SQL Assistant
เป็นเครื่องมือช่วยนักศึกษาวิเคราะห์คำสั่ง SQL
ตรวจสอบข้อผิดพลาด
อธิบายการทำงาน
และเสนอแนวทางปรับปรุงคำสั่ง SQL
โดยใช้ Google Gemini API


## Target users

- University students in a databases / SQL course, using the tool to check
  homework queries or study for exams.
- Instructors/TAs who want a quick "does this query at least parse and make
  sense" pass before manually grading.
- Anyone learning SQL who wants immediate, example-specific feedback rather
  than generic documentation.

Assumed technical level: comfortable writing basic-to-intermediate SQL, but
not necessarily confident about correctness, performance, or dialect quirks.

## Input

- A single SQL statement (any standard DDL/DML — the assignment focuses on
  SELECT-style analysis, but the app does not hard-block other statement
  types).
- Optionally wrapped in a ` ```sql ` fenced block — the app strips this in
  the prompt by asking Gemini to treat the block as the query.
- Delivered as one message per chat turn via Streamlit's `st.chat_input`.
- Follow-up natural-language messages are technically possible (the chat
  session has no client-side content filtering), though the system
  instruction is written assuming each turn is a SQL query to analyze.

## Output

A single JSON object per turn, validated against the `SQLAnalysis` schema:

| Field | Type | Meaning |
|---|---|---|
| `valid` | bool | Whether the query is syntactically valid |
| `difficulty` | enum: Easy / Medium / Hard | Estimated authoring/comprehension difficulty |
| `explanation` | string | Plain-English description of what the query does |
| `syntax_errors` | string[] | Concrete syntax problems found (empty if valid) |
| `suggested_fixes` | string[] | Fixes for each error, same order as `syntax_errors` |
| `optimized_sql` | string | Corrected and/or optimized rewrite of the query |
| `tips` | string[] | 1-5 actionable optimization / best-practice tips |

Rendered in the UI as: a validity/difficulty badge line, an explanation
paragraph, error/fix callouts (only if present), a syntax-highlighted SQL
block for the optimized query, a bulleted tips list, and a collapsible "Raw
JSON" view for transparency/debugging.

## Architecture

```
┌─────────────────────────┐        ┌─────────────────────────────┐
│        Streamlit UI       │        │   google-genai SDK client    │
│  (app.py, single file)    │        │                               │
│                            │        │  client.chats.create(         │
│ Sidebar:                  │        │    model, config)             │
│  - model select            │──────▶│                               │
│  - temperature slider      │        │  chat.send_message_stream(    │
│  - thinking toggle         │        │    prompt) -> chunk iterator  │
│  - clear chat              │        │                               │
│                            │◀──────│  GenerateContentConfig(         │
│ Main:                      │        │    system_instruction,        │
│  - chat_input (SQL)        │        │    temperature,               │
│  - streamed raw JSON        │        │    response_mime_type=json,   │
│  - parsed pretty view       │        │    response_schema=SQLAnalysis,│
│  - conversation history     │        │    thinking_config?)          │
└─────────────────────────┘        └─────────────────────────────┘
```

- **Single-file app** (`app.py`) split into clearly separated sections:
  constants/schema, backend helpers (talk to `google.genai`), frontend
  helpers (Streamlit rendering), and `main()`.
- **State**: Streamlit's `st.session_state` holds the visible transcript
  (`history`), the active chat object (`chat`), and a `chat_key` fingerprint
  of (model, temperature, thinking) used to detect when settings changed and
  a new chat session must be created — Gemini chat sessions are immutable
  once created.
- **Client caching**: `genai.Client` is created once per API key via
  `st.cache_resource`, avoiding reconnecting on every Streamlit rerun.

## Prompt design

### System Instruction

The system instruction (see `SYSTEM_INSTRUCTION` in `app.py`) does three
jobs:
1. Sets the persona and audience ("expert SQL tutor... used by university
   students").
2. Fixes the input/output contract: one query in, one JSON object out, no
   surrounding prose.
3. Gives explicit, numbered grading rules for each field (what counts as
   "Hard", how to decide `valid`, how to order `suggested_fixes` relative to
   `syntax_errors`) — this reduces ambiguity that would otherwise cause
   inconsistent grading across turns/temperatures.

Per-turn user messages are deliberately minimal ("Analyze this SQL query:
```sql ... ```") because all the heavy behavioral instruction already lives
in the system instruction — this keeps token usage low and keeps behavior
consistent turn to turn.

### JSON Schema

Defined as a Pydantic model (`SQLAnalysis`) with a nested `Difficulty` enum,
passed directly as `response_schema` in `GenerateContentConfig` alongside
`response_mime_type="application/json"`. Using Pydantic (rather than a
hand-written JSON Schema dict) gives:
- A single source of truth for both the API contract and the app's parsing
  (`SQLAnalysis.model_validate_json(raw_text)`), so schema drift can't cause
  silent bugs.
- Field-level `description=` text that also acts as extra guidance to the
  model about the intent of each field.
- Type-checked, defaulted list fields (`syntax_errors`, `suggested_fixes`,
  `tips` all default to `[]`) so downstream rendering code never has to
  null-check.

## Features used from Gemini

- **System Instructions** — `GenerateContentConfig(system_instruction=...)`.
- **Structured Output** — `response_mime_type="application/json"` +
  `response_schema=SQLAnalysis`.
- **Multi-turn Chat** — `client.chats.create(model=..., config=...)`, then
  repeated `chat.send_message_stream(...)` calls per turn.
- **Streaming** — iterating the generator returned by
  `send_message_stream`, reading `.text` off each chunk.
- **Temperature** — `GenerateContentConfig(temperature=...)`, exposed as a
  sidebar slider.
- **Code Execution** — `types.Tool(code_execution=types.ToolCodeExecution())`
  on a separate `client.models.generate_content(...)` call (outside the chat
  session, since `tools` and `response_schema` cannot be combined in one
  request). Gemini writes and runs real Python/sqlite3 to build sample
  tables and actually execute the optimized query, verifying it works
  rather than just asserting it does.
- **Multimodality** — an optional schema/ER-diagram image, sent as
  `types.Part.from_bytes(data=..., mime_type=...)` alongside the text prompt
  in a single multimodal chat message, so Gemini can cross-check referenced
  tables/columns against the real schema.
- **Thinking Level (bonus)** — `types.ThinkingConfig(thinking_budget=1024)`,
  toggled in the sidebar, applied only to models in the 2.5+/3.x family.

Not used in this version (documented as future improvements below):
Function Calling, Grounding Search.

## Temperature choice

Default: **0.2**. Rationale (expanded from README):

- The task is evaluative/analytical (is this valid? how hard is it? what's
  the best rewrite?), not open-ended/creative — low temperature favors the
  model's highest-confidence, most consistent answer, which is what a
  correctness/learning tool should optimize for.
- Structured JSON generation is itself more reliable at lower temperatures;
  higher temperatures increase the chance of subtly malformed or
  inconsistent structured output (e.g. contradicting itself between
  `valid` and `syntax_errors`).
- The slider still allows 0.0–1.0 so the assignment's temperature
  requirement is demonstrably tunable, and so students can observe firsthand
  how raising temperature affects consistency — itself a useful lesson.

## Future improvements

- **Function Calling**: expose a `run_explain_plan(sql)` tool backed by a
  real (sandboxed) database (e.g. SQLite) so Gemini can request an actual
  EXPLAIN plan instead of guessing performance characteristics.
- **Grounding with Search**: for dialect-specific questions ("does
  PostgreSQL support this syntax?"), ground answers in current
  documentation rather than relying solely on model knowledge.
- **Schema-aware analysis using the student's real data**: currently the
  Code Execution check invents plausible sample tables; letting the student
  also paste real `CREATE TABLE` statements (in addition to the optional
  schema image) would let the sandbox use the actual schema instead of a
  guess.
- **Session persistence**: save/reload chat history across browser
  restarts (currently in-memory only, per Streamlit session).
- **Multi-query batch mode**: paste a whole `.sql` file and get one
  analysis per statement.
