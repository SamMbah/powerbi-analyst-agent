"""
Power BI Analyst Agent — ReAct loop with DAX generation.

Designed as a generator so it can stream events to the FastAPI SSE
endpoint in real time. Each yield is a dict that becomes one SSE event.

Event types:
  {"type": "thinking",  "thought": "..."}
  {"type": "dax",       "query": "..."}
  {"type": "result",    "data": "..."}
  {"type": "chart",     "image": "<base64 png>"}
  {"type": "complete",  "answer": "..."}
  {"type": "error",     "message": "..."}
"""

import json
import re
import os
import base64
import tempfile
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from anthropic import Anthropic
from .state import AgentState
from .client import execute_dax


SYSTEM_PROMPT = """You are an expert Power BI analyst and DAX developer.
You have access to a Power BI dataset and can run DAX queries against it.

On each turn you receive:
- The user's business question
- The full dataset schema (tables, columns, measures)
- History of queries run and their results so far

RESPONSE FORMAT — valid JSON only, one of:

To run a DAX query:
{
  "thought": "what I need to find out and why",
  "dax": "EVALUATE ..."
}

To generate a chart from the last query result:
{
  "thought": "I have the data, now I will visualise it",
  "chart": {
    "type": "bar|line|pie",
    "x_column": "column name",
    "y_column": "column name",
    "title": "chart title"
  }
}

When you have a complete answer:
{
  "thought": "I have enough to answer",
  "is_complete": true,
  "final_answer": "detailed business answer with specific numbers"
}

DAX RULES:
- Always start queries with EVALUATE
- Use SUMMARIZECOLUMNS for grouped aggregations
- Use CALCULATE to apply filters to measures
- Reference measures as [MeasureName], columns as Table[Column]
- For top N: TOPN(N, table, [measure], DESC)
- Always include a print-friendly alias: "Revenue", [Total Revenue]

DATE FILTERING — CRITICAL:
- NEVER use YEAR()/MONTH() inside SUMMARIZECOLUMNS filter arguments — they break filter context
- CORRECT pattern for date range in SUMMARIZECOLUMNS:
    EVALUATE SUMMARIZECOLUMNS(
        Orders[Product],
        FILTER(ALL(Orders), Orders[Order Date] >= DATE(2013,7,1) && Orders[Order Date] < DATE(2013,10,1)),
        "Revenue", SUM(Orders[Sales])
    )
- CORRECT pattern using CALCULATE:
    EVALUATE ADDCOLUMNS(VALUES(Orders[Product]), "Revenue",
        CALCULATE(SUM(Orders[Sales]),
            Orders[Order Date] >= DATE(2013,7,1),
            Orders[Order Date] < DATE(2013,10,1)))
- Use DATE(year, month, day) for literal dates
- To find "last quarter": first query MAX(Orders[Order Date]) to find the data's max date, then derive the quarter from that

ANALYSIS RULES:
- Start with an overview query, then drill into dimensions
- If a query errors, fix the specific issue in the next step
- Never repeat a query you've already run
- Final answers must cite specific numbers from query results
- If you have partial results after 8+ steps, answer with what you have rather than retrying indefinitely

WHEN SCHEMA IS UNKNOWN OR EMPTY:
- Your very first step must be schema exploration
- Try: EVALUATE TOPN(1, 'Orders') — if it errors, that table doesn't exist
- Try common names: Orders, Sales, Fact Sales, Products, Customers, Dates
- Once you find valid tables, use TOPN(0, 'TableName') to see all columns
- Only start answering the actual question once you know the data model
"""


def format_schema(schema: dict) -> str:
    lines = []
    tables = [t["TableName"] for t in schema.get("tables", [])
              if not t["TableName"].startswith("$")]
    lines.append(f"TABLES: {', '.join(tables)}")
    lines.append("\nCOLUMNS:")
    cols_by_table: dict[str, list] = {}
    for col in schema.get("columns", []):
        t = col.get("TableName", "")
        if not t.startswith("$"):
            cols_by_table.setdefault(t, []).append(
                f"{col.get('ColumnName')} ({col.get('DataType')})"
            )
    for t, cols in cols_by_table.items():
        lines.append(f"  {t}: {', '.join(cols)}")
    if schema.get("measures"):
        lines.append("\nMEASURES:")
        for m in schema["measures"]:
            lines.append(f"  [{m['MeasureName']}] = {m.get('Expression','')[:80]}")
    return "\n".join(lines)


def build_prompt(state: AgentState) -> str:
    parts = [
        f"Question: {state.question}",
        f"\nDataset: {state.dataset_name} (workspace: {state.workspace_name})",
        f"\nSchema:\n{state.schema_summary}",
    ]
    if state.steps:
        parts.append("\n--- Analysis history ---")
        for i, step in enumerate(state.steps, 1):
            parts.append(f"\nStep {i} — Thought: {step['thought']}")
            if step.get("dax"):
                parts.append(f"DAX:\n{step['dax']}\nResult:\n{step['result']}")
            if step.get("had_chart"):
                parts.append("[Chart was generated for this step]")
    parts.append("\nWhat is your next step?")
    return "\n".join(parts)


def df_to_string(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "Query returned no rows."
    sample = df.head(25)
    return sample.to_string(index=False)


def make_chart_base64(df: pd.DataFrame, spec: dict) -> str | None:
    """Generate a chart and return it as a base64 PNG string."""
    try:
        x, y = spec.get("x_column"), spec.get("y_column")
        kind = spec.get("type", "bar")
        title = spec.get("title", "")

        if x not in df.columns or y not in df.columns:
            return None

        fig, ax = plt.subplots(figsize=(10, 5))
        nums = pd.to_numeric(df[y], errors="coerce")

        if kind == "bar":
            ax.bar(df[x].astype(str), nums, color="#F59E0B", edgecolor="#1A1A2E")
            plt.xticks(rotation=30, ha="right")
        elif kind == "line":
            ax.plot(df[x].astype(str), nums, marker="o", color="#F59E0B", linewidth=2)
            plt.xticks(rotation=30, ha="right")
        elif kind == "pie":
            ax.pie(nums.fillna(0), labels=df[x].astype(str),
                   autopct="%1.1f%%", startangle=90)

        fig.patch.set_facecolor("#0F0F0F")
        ax.set_facecolor("#1A1A2E")
        ax.set_title(title, color="#E2E8F0", fontsize=13, fontweight="bold", pad=12)
        if kind != "pie":
            ax.set_xlabel(x, color="#94A3B8")
            ax.set_ylabel(y, color="#94A3B8")
            ax.tick_params(colors="#94A3B8")
            for spine in ax.spines.values():
                spine.set_edgecolor("#2D2D4E")

        plt.tight_layout()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        plt.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close()

        with open(tmp.name, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        plt.close()
        return None


def parse_response(text: str) -> dict:
    text = re.sub(r"```json\s*|\s*```", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    raise ValueError(f"No JSON in response: {text}")


def stream_analysis(state: AgentState, schema: dict):
    """
    Generator that runs the ReAct loop and yields SSE event dicts.
    FastAPI's StreamingResponse iterates this and sends each event to the browser.
    """
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    state.schema_summary = format_schema(schema)
    last_df: pd.DataFrame | None = None

    for iteration in range(state.max_iterations):
        try:
            has_clean_results = (
                state.steps
                and state.steps[-1].get("result")
                and not state.steps[-1]["result"].startswith("ERROR")
            )
            # Haiku for DAX generation — fast and cheap.
            # Promote to Sonnet once we have clean results ready to synthesise.
            model = "claude-sonnet-4-6" if has_clean_results else "claude-haiku-4-5-20251001"

            # Warn the agent when it's approaching the limit so it wraps up
            steps_left = state.max_iterations - iteration
            urgency = (
                f"\n\n⚠️ Only {steps_left} step(s) remaining. "
                "If you have any useful data, answer now with is_complete=true."
                if steps_left <= 3 else ""
            )

            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(state) + urgency}],
            )
            parsed = parse_response(response.content[0].text)
        except Exception as e:
            yield {"type": "error", "message": str(e)}
            return

        thought = parsed.get("thought", "")
        yield {"type": "thinking", "thought": thought}

        if parsed.get("is_complete"):
            state.is_complete = True
            state.final_answer = parsed.get("final_answer", "")
            yield {"type": "complete", "answer": state.final_answer}
            return

        # Chart generation step
        if parsed.get("chart") and last_df is not None:
            img = make_chart_base64(last_df, parsed["chart"])
            if img:
                if state.steps:
                    state.steps[-1]["had_chart"] = True
                yield {"type": "chart", "image": img, "title": parsed["chart"].get("title", "")}
            continue

        # DAX execution step
        dax = parsed.get("dax", "").strip()
        if not dax:
            yield {"type": "error", "message": "Agent returned no DAX query."}
            return

        yield {"type": "dax", "query": dax}

        try:
            last_df = execute_dax(state.workspace_id, state.dataset_id, dax)
            result_str = df_to_string(last_df)
        except ValueError as e:
            result_str = f"ERROR: {e}"
            last_df = None

        yield {"type": "result", "data": result_str}

        state.steps.append({
            "thought": thought,
            "dax": dax,
            "result": result_str,
            "had_chart": False,
        })

    if not state.is_complete:
        yield {"type": "complete", "answer": "Reached iteration limit. See steps above for findings."}
