## AI Usage in this Project

โปรเจกต์นี้ใช้ AI เป็นผู้ช่วยในการพัฒนาในส่วนต่อไปนี้

- ช่วยออกแบบแนวคิดของโปรเจกต์
- ช่วยออกแบบหน้าตาโปรแกรมด้วย Streamlit
- ช่วยร่าง System Instruction ของ Gemini
- ช่วยออกแบบโครงสร้างข้อมูลแบบ JSON
- ช่วยจัดทำเอกสาร README และ SPEC
- ช่วยตรวจสอบและปรับปรุงโค้ด Python

ผู้พัฒนาเป็นผู้เลือกฟีเจอร์ ปรับแก้ ทดสอบ และตรวจสอบความถูกต้องของโปรแกรมให้เป็นที่การบ้านกำหนด

## Features

| Requirement | Where it's implemented |
|---|---|
| ✅ System Instructions | `SYSTEM_INSTRUCTION` in [app.py](app.py), passed via `GenerateContentConfig(system_instruction=...)` |
| ✅ Structured Output (JSON Schema) | `SQLAnalysis` Pydantic model, passed as `response_schema` with `response_mime_type="application/json"` |
| ✅ Multi-turn Chat | `client.chats.create(...)` — one chat session per (model, temperature, thinking) combo |
| ✅ Streaming Response | `chat.send_message_stream(...)`, rendered token-by-token into the chat bubble |
| ✅ Temperature | Sidebar slider, passed via `GenerateContentConfig(temperature=...)` |
| ✅ Code Execution | "Verify with Code Execution" checkbox → separate `generate_content` call with `types.Tool(code_execution=types.ToolCodeExecution())`; Gemini writes + runs real Python/sqlite3 to confirm the optimized query executes |
| ✅ Multimodality | Optional schema/ER-diagram image upload → sent as a `types.Part.from_bytes(...)` alongside the text prompt in the chat message |
| 🎁 Thinking Level (bonus) | Sidebar checkbox → `types.ThinkingConfig(thinking_budget=1024)` on models that support it |

## Application flow

1. User pastes a SQL query into the chat input.
2. The app wraps it in a short instruction ("Analyze this SQL query: ...")
   and sends it to the current multi-turn `chat` session.
3. If the student uploaded a schema/ER-diagram image, it's attached to that
   same message as an image part (Multimodality) so Gemini can cross-check
   the query's tables/columns against the real schema.
4. Gemini streams back **JSON only**, matching the `SQLAnalysis` schema:
   `valid`, `difficulty`, `explanation`, `syntax_errors`, `suggested_fixes`,
   `optimized_sql`, `tips`.
5. The app renders the streaming raw JSON live, then re-renders it as a
   pretty UI (status badges, error/fix callouts, a SQL code block, a tips
   list) once the stream completes, plus a "Raw JSON" expander.
6. If "Verify with Code Execution" is enabled, the app makes one extra,
   separate `generate_content` call with the Code Execution tool: Gemini
   writes Python that builds a scratch SQLite database with plausible sample
   tables, actually runs the optimized query against it, and reports whether
   it really executes — shown under "🧪 Code Execution Verification".
7. Because it's a real chat session, follow-up messages ("what if I add an
   index on `orders.customer_id`?") keep the prior turns as context.

---

## Installation

```bash
pip install -r requirements.txt
```

Copy the environment template and add your [Gemini API key](https://aistudio.google.com/apikey):

```bash
cp .env.example .env
```

Edit `.env`:

```
GEMINI_API_KEY=your-api-key-here
```

## How to run

```bash
streamlit run app.py
```

Then open the URL Streamlit prints (usually `http://localhost:8501`).

### Model availability note

The sidebar defaults to `gemini-2.5-flash` per the assignment brief. If that
model name isn't yet enabled for your API key/region, switch the "Model"
dropdown to `gemini-2.5-flash` (fast, cheap, great for this use case) or
`gemini-2.5-pro` — the app code does not need any changes, since the model
name is just a string passed to `client.chats.create(model=...)`.

---

## Example Input

```sql
SELECT o.id, c.name, SUM(o.total) 
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.status = 'paid'
GROUP BY c.name
```

## Example Output

The app renders this as a formatted panel, but the underlying streamed JSON
looks like:

```json
{
  "valid": true,
  "difficulty": "Medium",
  "explanation": "This query joins the orders and customers tables on customer_id, keeps only orders with status 'paid', and returns each customer's name alongside the total amount they've paid across all their orders, grouped by customer name.",
  "syntax_errors": [],
  "suggested_fixes": [],
  "optimized_sql": "SELECT o.id, c.name, SUM(o.total) AS total_paid\nFROM orders o\nJOIN customers c ON o.customer_id = c.id\nWHERE o.status = 'paid'\nGROUP BY o.id, c.name;",
  "tips": [
    "Alias SUM(o.total) as total_paid so the output column has a meaningful name.",
    "Group by o.id in addition to c.name to avoid ambiguity if two customers share a name.",
    "Add an index on orders(customer_id, status) to speed up the join and filter.",
    "If you only need aggregated totals per customer (not per order id), drop o.id from SELECT and GROUP BY."
  ]
}
```

---

## Gemini feature explanations

### System Instructions

The entire behavior contract for the model — "you are a SQL tutor, always
reply with JSON matching this schema, follow these 7 rules" — lives in the
`SYSTEM_INSTRUCTION` constant in [app.py](app.py) and is passed once via
`GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)`. This keeps
the actual per-turn user message short (just the SQL) while keeping the
model's behavior consistent across every turn of the chat.

### JSON Schema (Structured Output)

Rather than parsing free-text output, the app defines an `SQLAnalysis`
Pydantic model (with an `Difficulty` enum for the three allowed values) and
passes it directly as `response_schema`, together with
`response_mime_type="application/json"`. The `google-genai` SDK converts the
Pydantic model into a JSON Schema under the hood and constrains Gemini's
output to match it — so `SQLAnalysis.model_validate_json(raw_text)` reliably
succeeds without brittle prompt-only JSON instructions.

### Streaming

`chat.send_message_stream(prompt)` returns an iterator of chunks as Gemini
generates its response. The app accumulates `chunk.text` into a buffer and
re-renders it inside a `st.empty()` placeholder on every chunk (with a
trailing `█` cursor), giving a live "typing" effect for the raw JSON before
the final parsed view replaces it. This is what `stream_analysis()` in
[app.py](app.py) does.

### Multi-turn Chat

`client.chats.create(model=..., config=...)` opens a stateful chat session
that Gemini's SDK keeps history for internally — every call to
`chat.send_message_stream(...)` automatically includes prior turns as
context, so a student can ask a follow-up like "explain the JOIN in the
optimized version" and Gemini remembers the original query. The app keeps
one chat session in `st.session_state`, and only starts a new one if the
user changes the model, temperature, or thinking toggle (since those are
bound to the session at creation time).

### Temperature — why 0.2 by default

The task is **analytical, not creative**: detecting syntax errors,
classifying difficulty, and rewriting SQL correctly all have a "more right"
and "more wrong" answer. A low default temperature (**0.2**) biases Gemini
toward its most confident, consistent completion — important for a tool
students will trust for correctness feedback, and important for structured
output in general (lower temperature reduces the odds of the model drifting
into a nearly-valid-but-malformed JSON shape). The sidebar still exposes the
full 0.0–1.0 range so students can see for themselves how raising the
temperature increases variability in phrasing, difficulty labeling, and
occasionally in schema adherence — a useful, hands-on illustration of what
temperature actually does.

### Code Execution

Gemini's `code_execution` tool lets the model write and run Python in a real
sandbox. The API does not allow mixing `tools` with `response_schema` in one
request, so this is a **second, independent call** (`run_code_execution_check`
in [app.py](app.py)) made with `client.models.generate_content(...)` outside
the chat session, triggered only when the sidebar checkbox is on. Gemini is
asked to invent plausible sample tables matching the query's table names,
insert a few rows, and actually execute the (optimized) query against them —
turning "I think this query is valid" into "I ran it and it returned rows."
The response parts are inspected for `part.executable_code` (the code Gemini
wrote) and `part.code_execution_result` (the real stdout), both rendered
under "🧪 Code Execution Verification".

### Multimodality

The main page includes an optional image uploader for a database schema or
ER diagram. When a query is submitted with an image attached, the app builds
the chat message as a list `[prompt_text, image_part]` where `image_part =
types.Part.from_bytes(data=..., mime_type=...)` — the `google-genai` SDK
accepts a list of parts (text + inline image bytes) as a single multimodal
turn. The prompt explicitly asks Gemini to check the query's tables/columns
against the diagram and flag mismatches.

### Thinking (bonus)

When enabled and the selected model supports it (2.5+/3.x family), the app
sets `types.ThinkingConfig(thinking_budget=1024)` on the request. This gives
Gemini a small internal reasoning budget before it commits to an answer,
which can improve accuracy on trickier queries (e.g. spotting a subtle
correlated-subquery bug) at the cost of a bit of extra latency.

---

## Project structure

```
Gemini-SQL-Assistant/
├── app.py              # Streamlit app + all Gemini/google-genai backend logic
├── requirements.txt
├── README.md
├── SPEC.md
├── .env.example
├── .gitignore
└── assets/             # Reserved for screenshots/diagrams
```
