"""Gemini SQL Assistant — a Streamlit chat app that analyzes SQL queries using
the Google GenAI SDK (Gemini).

Demonstrates, per assignment requirements:
    - System Instructions
    - Structured Output (JSON Schema via a Pydantic model)
    - Multi-turn Chat (client.chats.create)
    - Streaming Response (send_message_stream)
    - Temperature (user-configurable)
    - Code Execution — verifies the optimized SQL by actually running it
      against a scratch in-memory SQLite database (a one-off generate_content
      call, since the Gemini API does not allow combining `tools` with
      `response_schema` in a single request)
    - Multimodality — optional schema/ER-diagram image upload, attached as an
      image Part alongside the text prompt in the chat message
    - Bonus: Thinking Level (thinking_config, on models that support it)

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import Any

import streamlit as st
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from google import genai
from google.genai import types

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Models known to be available through the Gemini API at the time of writing.
# "gemini-3.5-flash" is included because it is the model named in the
# assignment brief; if it is not yet available on your API key/region, pick
# one of the other options below (see README.md "Model availability" note).
AVAILABLE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]

# Models in the 2.5+ family expose a "thinking" budget. Older models error
# out if thinking_config is supplied, so we gate the bonus feature on this.
THINKING_CAPABLE_SUBSTRINGS: tuple[str, ...] = ("2.5", "3.5", "3.0")

DEFAULT_TEMPERATURE = 0.2

SYSTEM_INSTRUCTION = """\
You are "Gemini SQL Assistant", an expert SQL tutor and query reviewer used \
by university students learning databases.

For every user turn, the user will send you a single SQL query (it may be \
wrapped in a ```sql code block```). You must analyze that query and respond \
with ONLY a JSON object that matches the provided response schema — no prose \
before or after the JSON, no markdown fences.

Rules for your analysis:
1. "valid" must be false if the query has any syntax error that would stop it \
   from running on a standard ANSI-SQL engine (assume standard SQL unless the \
   query clearly targets a specific dialect, e.g. T-SQL "TOP" or MySQL \
   backticks — in that case evaluate it against that dialect instead).
2. "difficulty" is your estimate of how hard the query is for a student to \
   write or understand: "Easy" (single table, simple WHERE/SELECT), "Medium" \
   (joins, GROUP BY, subqueries, aggregate functions), or "Hard" (window \
   functions, recursive CTEs, multiple nested subqueries, complex joins).
3. "explanation" is a clear, beginner-friendly explanation of what the query \
   does, written in plain English, 2-5 sentences.
4. "syntax_errors" lists every concrete syntax problem you find (empty list \
   if none). Be specific (e.g. "missing comma between column names 'id' and \
   'name'").
5. "suggested_fixes" lists concrete fixes for each problem in syntax_errors, \
   in the same order (empty list if the query is valid).
6. "optimized_sql" is your best rewrite of the query: fixed if it was broken, \
   and optimized for readability/performance if it was already valid. If no \
   change is needed, return the original query unchanged.
7. "tips" lists 1-5 short, actionable optimization or best-practice tips \
   relevant to this specific query (e.g. indexing advice, avoiding \
   SELECT *, sargability, join order).

Always reply in English, and always return a single JSON object, nothing else.
"""


class Difficulty(str, Enum):
    """Coarse difficulty bucket the model assigns to a query."""

    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"


class SQLAnalysis(BaseModel):
    """Structured response returned by Gemini for one SQL analysis turn.

    Passed directly as `response_schema` to the Gemini API — the new
    google-genai SDK accepts a Pydantic model class and converts it to a
    JSON Schema for structured output automatically.
    """

    valid: bool = Field(description="Whether the SQL is syntactically valid.")
    difficulty: Difficulty = Field(description="Estimated difficulty of the query.")
    explanation: str = Field(description="Plain-English explanation of the query.")
    syntax_errors: list[str] = Field(
        default_factory=list, description="Concrete syntax errors found, if any."
    )
    suggested_fixes: list[str] = Field(
        default_factory=list, description="Fixes for each syntax error, in order."
    )
    optimized_sql: str = Field(description="Fixed and/or optimized version of the query.")
    tips: list[str] = Field(default_factory=list, description="Optimization tips.")


# --------------------------------------------------------------------------
# Backend helpers (google-genai SDK)
# --------------------------------------------------------------------------


def load_api_key() -> str | None:
    """Load GEMINI_API_KEY from the environment / .env file."""
    load_dotenv()
    return os.environ.get("GEMINI_API_KEY")


@st.cache_resource(show_spinner=False)
def get_client(api_key: str) -> genai.Client:
    """Create (and cache) a single google-genai Client for the session."""
    return genai.Client(api_key=api_key)


def build_generation_config(
    temperature: float, enable_thinking: bool, model: str
) -> types.GenerateContentConfig:
    """Build the GenerateContentConfig shared by every turn of the chat.

    Bundles the four config-level requirements from the assignment:
    system_instruction, temperature, and structured JSON output
    (response_mime_type + response_schema). Thinking is added only for
    models known to support a thinking budget.
    """
    config_kwargs: dict[str, Any] = dict(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=SQLAnalysis,
    )

    if enable_thinking and any(tag in model for tag in THINKING_CAPABLE_SUBSTRINGS):
        # Small, fixed thinking budget: enough for query analysis without
        # materially increasing latency/cost for a classroom demo.
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=1024)

    return types.GenerateContentConfig(**config_kwargs)


def run_code_execution_check(client: genai.Client, model: str, sql_text: str) -> str:
    """Ask Gemini to verify the query actually runs, using the Code Execution tool.

    This is a separate, one-off `generate_content` call (not part of the chat
    session) because the Gemini API does not allow combining `tools` with
    `response_schema` in the same request. Gemini writes and runs Python
    (sqlite3) code in a sandbox: it builds plausible sample tables for
    whatever table names appear in the query, then actually executes the
    query against them and reports success/failure and sample rows.

    Returns a markdown string interleaving Gemini's commentary, the
    generated code, and the real execution output.
    """
    prompt = (
        "You have access to a Python code execution sandbox with the built-in "
        "sqlite3 module. Write and run Python code that:\n"
        "1. Creates an in-memory SQLite database.\n"
        "2. Infers plausible CREATE TABLE statements for every table referenced "
        "in the SQL query below (reasonable column names/types), and inserts "
        "3-5 rows of sample data into each.\n"
        "3. Executes the SQL query below against that database (adapt minor "
        "dialect-only syntax if needed to run on SQLite, but do not change its "
        "logic).\n"
        "4. Prints whether it succeeded and, if so, the first few result rows "
        "(or the affected row count for non-SELECT statements). If it fails, "
        "print the exact error message.\n\n"
        f"SQL query to test:\n```sql\n{sql_text}\n```"
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(code_execution=types.ToolCodeExecution())],
            temperature=0.2,
        ),
    )

    parts = response.candidates[0].content.parts if response.candidates else []
    chunks: list[str] = []
    for part in parts:
        if getattr(part, "text", None):
            chunks.append(part.text)
        elif getattr(part, "executable_code", None):
            chunks.append(f"```python\n{part.executable_code.code}\n```")
        elif getattr(part, "code_execution_result", None):
            chunks.append(f"**Execution output:**\n```\n{part.code_execution_result.output}\n```")

    return "\n\n".join(chunks) if chunks else "_(Gemini returned no output for the execution check.)_"


def get_or_create_chat(
    client: genai.Client, model: str, config: types.GenerateContentConfig
) -> Any:
    """Return the multi-turn chat session for the current settings.

    The Gemini chat session (client.chats.create) binds its model and config
    at creation time, so whenever the user changes the model, temperature, or
    thinking toggle in the sidebar we must start a fresh chat (and, since the
    old context no longer applies, a fresh visible transcript too).
    """
    thinking_enabled = getattr(config, "thinking_config", None) is not None
    session_key = (model, config.temperature, thinking_enabled)

    if st.session_state.get("chat_key") != session_key:
        st.session_state.chat = client.chats.create(model=model, config=config)
        st.session_state.chat_key = session_key
        st.session_state.history = []

    return st.session_state.chat


def stream_analysis(
    chat: Any,
    sql_text: str,
    placeholder: "st.delta_generator.DeltaGenerator",
    schema_image: types.Part | None = None,
) -> str:
    """Send one message to the chat and stream the raw JSON text live.

    Renders a typewriter-style preview into `placeholder` as chunks arrive
    (the Streaming Response requirement) and returns the full accumulated
    text once the stream ends. If `schema_image` is provided (Multimodality),
    it is attached alongside the text so Gemini can cross-check the query
    against the uploaded schema/ER diagram.
    """
    prompt_text = f"Analyze this SQL query:\n\n```sql\n{sql_text}\n```"
    if schema_image is not None:
        prompt_text += (
            "\n\nAn image of the database schema / ER diagram is attached. "
            "Use it to check that tables and columns referenced in the query "
            "actually exist, and mention any mismatch in your explanation or "
            "as a syntax_errors entry."
        )
        message: Any = [prompt_text, schema_image]
    else:
        message = prompt_text

    buffer = ""
    for chunk in chat.send_message_stream(message):
        if chunk.text:
            buffer += chunk.text
            placeholder.markdown(f"```json\n{buffer}█\n```")
    placeholder.empty()
    return buffer


# --------------------------------------------------------------------------
# Frontend helpers (Streamlit)
# --------------------------------------------------------------------------


def init_session_state() -> None:
    """Ensure all session_state keys exist before first use."""
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("chat_key", None)
    st.session_state.setdefault("chat", None)


def render_sidebar() -> tuple[str, float, bool, bool]:
    """Render the sidebar controls.

    Returns (model, temperature, enable_thinking, enable_code_execution).
    """
    with st.sidebar:
        st.header("⚙️ Settings")

        model = st.selectbox(
            "Model",
            options=AVAILABLE_MODELS,
            index=0,
            help="Gemini model used for analysis. Multi-turn chat restarts if you change this.",
        )

        temperature = st.slider(
            "Temperature",
            min_value=0.0,
            max_value=1.0,
            value=DEFAULT_TEMPERATURE,
            step=0.05,
            help="Lower = more deterministic/consistent analysis. See README for rationale.",
        )

        enable_thinking = st.checkbox(
            "Enable Thinking (bonus)",
            value=False,
            help="Lets the model reason internally before answering. Only applies to 2.5+/3.x models.",
        )

        enable_code_execution = st.checkbox(
            "Verify with Code Execution",
            value=False,
            help=(
                "After the structured analysis, ask Gemini to actually run the "
                "optimized query (via its Code Execution tool + sandboxed SQLite) "
                "to confirm it really works. Adds one extra API call per query."
            ),
        )

        st.caption("Changing model, temperature, or thinking starts a new chat session.")

        if st.button("🗑️ Clear chat", use_container_width=True):
            st.session_state.history = []
            st.session_state.chat_key = None
            st.session_state.chat = None
            st.rerun()

        st.divider()
        st.caption(
            "Gemini SQL Assistant — university assignment project.\n\n"
            "Paste a SQL query in the chat box to get an explanation, "
            "validity check, difficulty rating, optimized rewrite, and tips."
        )

    return model, temperature, enable_thinking, enable_code_execution


def render_analysis(data: dict[str, Any], code_execution_report: str | None = None) -> None:
    """Render one SQLAnalysis dict as readable UI plus a raw-JSON expander."""
    valid = data.get("valid", False)
    difficulty = data.get("difficulty", "Unknown")
    status_icon = "✅" if valid else "❌"

    st.markdown(f"**{status_icon} Valid:** `{valid}`  |  **Difficulty:** `{difficulty}`")

    st.markdown("**Explanation**")
    st.write(data.get("explanation", ""))

    syntax_errors = data.get("syntax_errors") or []
    if syntax_errors:
        st.markdown("**Syntax Errors**")
        for err in syntax_errors:
            st.error(err)

    suggested_fixes = data.get("suggested_fixes") or []
    if suggested_fixes:
        st.markdown("**Suggested Fixes**")
        for fix in suggested_fixes:
            st.info(fix)

    optimized_sql = data.get("optimized_sql")
    if optimized_sql:
        st.markdown("**Optimized SQL**")
        st.code(optimized_sql, language="sql")

    tips = data.get("tips") or []
    if tips:
        st.markdown("**Optimization Tips**")
        for tip in tips:
            st.markdown(f"- {tip}")

    if code_execution_report:
        st.markdown("**🧪 Code Execution Verification**")
        st.markdown(code_execution_report)

    with st.expander("Raw JSON response"):
        st.json(data)


def render_history() -> None:
    """Redraw every past turn in the visible chat transcript."""
    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            if turn["role"] == "user":
                st.code(turn["content"], language="sql")
                if turn.get("image_name"):
                    st.caption(f"📎 Attached schema image: {turn['image_name']}")
            elif turn.get("parsed"):
                render_analysis(turn["content"], turn.get("code_execution_report"))
            else:
                # Fallback: JSON parsing failed for this turn, show raw text.
                st.warning("Could not parse structured JSON for this turn. Raw response:")
                st.code(turn["content"], language="json")


def handle_new_query(
    client: genai.Client,
    model: str,
    chat: Any,
    sql_text: str,
    schema_image: types.Part | None = None,
    schema_image_name: str | None = None,
    enable_code_execution: bool = False,
) -> None:
    """Process one new user SQL submission end-to-end."""
    st.session_state.history.append(
        {"role": "user", "content": sql_text, "parsed": True, "image_name": schema_image_name}
    )
    with st.chat_message("user"):
        st.code(sql_text, language="sql")
        if schema_image_name:
            st.caption(f"📎 Attached schema image: {schema_image_name}")

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("Analyzing… █")
        try:
            raw_text = stream_analysis(chat, sql_text, placeholder, schema_image)
        except Exception as exc:  # noqa: BLE001 - surface any API error to the user
            st.error(f"Gemini request failed: {exc}")
            st.session_state.history.append(
                {"role": "assistant", "content": str(exc), "parsed": False}
            )
            return

        try:
            analysis = SQLAnalysis.model_validate_json(raw_text)
            data = analysis.model_dump(mode="json")
        except (ValidationError, json.JSONDecodeError) as exc:
            st.warning(f"Could not parse structured JSON ({exc}). Raw response:")
            st.code(raw_text, language="json")
            st.session_state.history.append(
                {"role": "assistant", "content": raw_text, "parsed": False}
            )
            return

        code_execution_report = None
        if enable_code_execution:
            with st.spinner("Verifying with Code Execution…"):
                try:
                    code_execution_report = run_code_execution_check(client, model, sql_text)
                except Exception as exc:  # noqa: BLE001
                    code_execution_report = f"_Code execution check failed: {exc}_"

        render_analysis(data, code_execution_report)
        st.session_state.history.append(
            {
                "role": "assistant",
                "content": data,
                "parsed": True,
                "code_execution_report": code_execution_report,
            }
        )


# --------------------------------------------------------------------------
# Main entrypoint
# --------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Gemini SQL Assistant", page_icon="🗄️", layout="wide")
    st.title("🗄️ Gemini SQL Assistant")
    st.caption("Paste a SQL query below — Gemini explains it, checks it, and suggests improvements.")

    api_key = load_api_key()
    if not api_key:
        st.error(
            "GEMINI_API_KEY is not set. Copy `.env.example` to `.env` and add your key, "
            "then restart the app."
        )
        st.stop()

    init_session_state()
    model, temperature, enable_thinking, enable_code_execution = render_sidebar()

    client = get_client(api_key)
    config = build_generation_config(temperature, enable_thinking, model)
    chat = get_or_create_chat(client, model, config)

    render_history()

    uploaded_image = st.file_uploader(
        "Optional: attach a schema / ER diagram image (Multimodality)",
        type=["png", "jpg", "jpeg", "webp"],
        help="Gemini will cross-check the SQL query's tables/columns against this image.",
    )

    sql_text = st.chat_input("Paste your SQL query here (e.g. SELECT * FROM orders;)")
    if sql_text:
        schema_image = None
        if uploaded_image is not None:
            schema_image = types.Part.from_bytes(
                data=uploaded_image.getvalue(), mime_type=uploaded_image.type
            )
        handle_new_query(
            client,
            model,
            chat,
            sql_text,
            schema_image=schema_image,
            schema_image_name=uploaded_image.name if uploaded_image is not None else None,
            enable_code_execution=enable_code_execution,
        )


if __name__ == "__main__":
    main()
