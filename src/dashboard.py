"""
Dashboard planner and generator.

Phase 1 — Plan:  One Sonnet call returns KPI metrics + 4-6 chart specs,
                  including drill-down DAX templates for each chart.
Phase 2 — Build: Execute each KPI/chart DAX, yield SSE events.
                  KPIs come first (big number cards), then Plotly charts.
"""

import json
import re
import os
import pandas as pd
import plotly.graph_objects as go
from anthropic import Anthropic
from .client import execute_dax
from .agent import format_schema


PALETTE = ["#F59E0B", "#60A5FA", "#34D399", "#F87171", "#A78BFA", "#FB923C"]


def _hex_rgba(hex_color: str, alpha: float = 0.15) -> str:
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f"rgba({r},{g},{b},{alpha})"


PLANNER_PROMPT = """You are a Power BI dashboard designer and DAX expert.

Given a user's request and dataset schema, produce a JSON OBJECT (not array):

{
  "kpis": [
    {
      "title": "Total Revenue",
      "dax": "EVALUATE ROW(\\"Value\\", SUM(Orders[Sales]))",
      "format": "currency"
    }
  ],
  "charts": [
    {
      "title": "Revenue by Region",
      "dax": "EVALUATE SUMMARIZECOLUMNS(Orders[Region], \\"Revenue\\", SUM(Orders[Sales]))",
      "chart_type": "bar",
      "x_column": "Region",
      "y_column": "Revenue",
      "base_table": "Orders",
      "insight": "one sentence describing what to look for",
      "drill_next": "Category",
      "drill_dax_template": "EVALUATE SUMMARIZECOLUMNS(Orders[Category], FILTER(ALL(Orders), Orders[Region] = \\"{value}\\"), \\"Revenue\\", SUM(Orders[Sales]))"
    }
  ]
}

KPI RULES:
- Produce exactly 3 KPIs (key totals shown as big summary numbers)
- format: "currency" for money/sales/profit, "number" for counts, "percent" for ratios
- KPI DAX must return a single ROW with a column named "Value"
- Example: EVALUATE ROW("Value", SUM(Orders[Sales]))

CHART RULES:
- Produce 4-6 charts covering different angles of the data
- chart_type: bar | line | pie | area  — mix for visual variety
- base_table: the primary table name used in this chart's DAX (e.g. "Orders")
- drill_next: next-level dimension when a bar or pie slice is clicked
- drill_dax_template: valid DAX EVALUATE with {value} as the filter placeholder,
  using FILTER(ALL(Table), Table[Col] = "{value}")
- Set drill_next and drill_dax_template to null if no meaningful drill-down exists

DATE GROUPING — ALWAYS use GROUPBY + CURRENTGROUP() for date parts:
  By year:
    EVALUATE GROUPBY(ADDCOLUMNS(Orders, "_Yr", YEAR(Orders[Order Date])), [_Yr], "Revenue", SUMX(CURRENTGROUP(), Orders[Sales])) ORDER BY [_Yr]
  By month:
    EVALUATE GROUPBY(ADDCOLUMNS(Orders, "_Mo", FORMAT(Orders[Order Date], "YYYY-MM")), [_Mo], "Revenue", SUMX(CURRENTGROUP(), Orders[Sales])) ORDER BY [_Mo]

DATE FILTERING — use FILTER(ALL(...)) as a filter table in SUMMARIZECOLUMNS:
  EVALUATE SUMMARIZECOLUMNS(
    Orders[Region],
    FILTER(ALL(Orders), Orders[Order Date] >= DATE(2010,1,1) && Orders[Order Date] < DATE(2014,1,1)),
    "Revenue", SUM(Orders[Sales]))
  NEVER use YEAR()/MONTH() inside SUMMARIZECOLUMNS filter arguments.
"""


def inject_filter(dax: str, filter_table: str, filter_col: str, filter_val: str) -> str:
    """
    Wrap a DAX EVALUATE expression with CALCULATETABLE to apply a cross-filter.
    Works for SUMMARIZECOLUMNS, GROUPBY, ROW, and most other table expressions.
    Preserves any trailing ORDER BY clause.
    """
    dax = dax.strip()
    order_by = ""
    pos = dax.upper().rfind(" ORDER BY ")
    if pos > 0:
        order_by = dax[pos:]
        dax = dax[:pos].rstrip()
    body = dax[8:].strip() if dax.upper().startswith("EVALUATE") else dax
    safe = filter_val.replace('"', '""')
    return f"EVALUATE CALCULATETABLE({body}, '{filter_table}'[{filter_col}] = \"{safe}\")" + order_by


def _probe_date_range(workspace_id: str, dataset_id: str, schema: dict) -> str:
    """
    Returns a context string with the actual date range and row count
    so the planner uses correct dates and avoids over-narrow filters.
    """
    date_cols = []
    for col in schema.get("columns", []):
        name  = col.get("ColumnName", "")
        dtype = col.get("DataType", "")
        if dtype in ("DateTime", "Date") or "date" in name.lower():
            date_cols.append((col["TableName"], name))

    for table, col in date_cols[:4]:
        try:
            dax = (
                f"EVALUATE ROW("
                f"\"MinDate\", FORMAT(MIN('{table}'[{col}]), \"YYYY-MM-DD\"), "
                f"\"MaxDate\", FORMAT(MAX('{table}'[{col}]), \"YYYY-MM-DD\"), "
                f"\"TotalRows\", COUNTROWS('{table}'))"
            )
            df = execute_dax(workspace_id, dataset_id, dax)
            if df is None or df.empty:
                continue
            mn    = str(df.iloc[0, 0])
            mx    = str(df.iloc[0, 1])
            total = int(float(str(df.iloc[0, 2]))) if len(df.columns) > 2 else 0
            if not mn or mn == "nan":
                continue

            if total < 300:
                density = (
                    f"⚠️ SMALL DATASET — only {total} rows total. "
                    "Do NOT filter to a single quarter (may return 0-1 rows). "
                    "Use the FULL date range or group by year instead."
                )
            elif total < 2000:
                density = (
                    f"Dataset has {total} rows. Prefer year-level grouping. "
                    "Only filter to a quarter if the user explicitly needs it."
                )
            else:
                density = f"Dataset has {total} rows — quarter or month filters are fine."

            return (
                f"Actual data date range: {mn} to {mx} "
                f"(column: {table}[{col}]). {density} "
                f"Use DATE() literals derived from this range — never assume today's date."
            )
        except Exception:
            continue
    return ""


def _base_layout(x_title: str = "", y_title: str = "") -> dict:
    return dict(
        paper_bgcolor="#161622",
        plot_bgcolor="#1E1E30",
        font=dict(color="#E2E8F0", size=11, family="Inter, system-ui, sans-serif"),
        title=dict(text=""),
        margin=dict(l=52, r=24, t=16, b=60),
        xaxis=dict(
            gridcolor="#2A2A40", linecolor="#2A2A40",
            tickfont=dict(color="#94A3B8", size=10),
            title=dict(text=x_title, font=dict(color="#64748B", size=10)),
        ),
        yaxis=dict(
            gridcolor="#2A2A40", linecolor="#2A2A40",
            tickfont=dict(color="#94A3B8", size=10),
            title=dict(text=y_title, font=dict(color="#64748B", size=10)),
        ),
        hoverlabel=dict(bgcolor="#0D0D1A", font_color="#E2E8F0", bordercolor="#2A2A40"),
        showlegend=False,
        dragmode="zoom",
        modebar=dict(bgcolor="rgba(0,0,0,0)", color="#64748B", activecolor="#F59E0B"),
    )


def make_chart(df: pd.DataFrame, spec: dict, index: int) -> str | None:
    """Build an interactive Plotly chart and return it as a JSON string."""
    try:
        x_col = spec.get("x_column")
        y_col = spec.get("y_column")
        kind  = spec.get("chart_type", "bar")
        color = PALETTE[index % len(PALETTE)]

        col_map = {c.lower(): c for c in df.columns}
        x_col = col_map.get(x_col.lower(), x_col) if x_col else df.columns[0]
        y_col = col_map.get(y_col.lower(), y_col) if y_col else df.columns[-1]

        if x_col not in df.columns or y_col not in df.columns:
            return None

        nums   = pd.to_numeric(df[y_col], errors="coerce")
        labels = df[x_col].astype(str)

        if kind == "bar":
            many_cats = len(labels) > 7
            text_vals = [f"{v:,.0f}" if pd.notna(v) else "" for v in nums]

            if many_cats:
                order = nums.argsort()
                trace = go.Bar(
                    y=labels.iloc[order], x=nums.iloc[order],
                    orientation="h",
                    marker=dict(color=color, line=dict(color="#0D0D0D", width=0.5)),
                    text=[text_vals[i] for i in order],
                    textposition="outside",
                    textfont=dict(color="#94A3B8", size=9),
                    hovertemplate="<b>%{y}</b><br>%{x:,.2f}<extra></extra>",
                )
                layout = _base_layout(y_col, "")
                layout["yaxis"]["automargin"] = True
                layout["margin"]["l"] = 160
                layout["margin"]["r"] = 60
                layout["xaxis"]["title"]["text"] = y_col
            else:
                trace = go.Bar(
                    x=labels, y=nums,
                    marker=dict(color=color, line=dict(color="#0D0D0D", width=0.5)),
                    text=text_vals,
                    textposition="outside",
                    textfont=dict(color="#94A3B8", size=9),
                    hovertemplate="<b>%{x}</b><br>%{y:,.2f}<extra></extra>",
                )
                layout = _base_layout(x_col, y_col)
            layout["bargap"] = 0.25
            fig = go.Figure(data=[trace], layout=layout)

        elif kind == "line":
            trace = go.Scatter(
                x=labels, y=nums, mode="lines+markers",
                line=dict(color=color, width=2),
                marker=dict(color=color, size=5),
                fill="tozeroy", fillcolor=_hex_rgba(color, 0.12),
                hovertemplate="<b>%{x}</b><br>%{y:,.2f}<extra></extra>",
            )
            fig = go.Figure(data=[trace], layout=_base_layout(x_col, y_col))

        elif kind == "pie":
            trace = go.Pie(
                labels=labels, values=nums.fillna(0),
                marker=dict(colors=PALETTE[:len(df)],
                            line=dict(color="#161622", width=2)),
                textfont=dict(color="#E2E8F0", size=10),
                textinfo="percent+label",
                hovertemplate="<b>%{label}</b><br>%{value:,.2f} (%{percent})<extra></extra>",
                hole=0.38,
            )
            pie_layout = _base_layout()
            pie_layout.update(
                plot_bgcolor="#161622", showlegend=True,
                legend=dict(font=dict(color="#94A3B8", size=10), orientation="v"),
                margin=dict(l=16, r=16, t=16, b=16),
            )
            pie_layout.pop("xaxis", None)
            pie_layout.pop("yaxis", None)
            fig = go.Figure(data=[trace], layout=pie_layout)

        elif kind == "area":
            trace = go.Scatter(
                x=labels, y=nums, mode="lines",
                line=dict(color=color, width=2),
                fill="tozeroy", fillcolor=_hex_rgba(color, 0.30),
                hovertemplate="<b>%{x}</b><br>%{y:,.2f}<extra></extra>",
            )
            fig = go.Figure(data=[trace], layout=_base_layout(x_col, y_col))

        else:
            return None

        return fig.to_json()

    except Exception:
        return None


def plan_dashboard(
    request: str, schema: dict, client: Anthropic, date_context: str = ""
) -> dict:
    """Ask Claude to plan KPIs + chart specs. Returns {"kpis": [...], "charts": [...]}."""
    schema_text = format_schema(schema)
    body = f"Dashboard request: {request}\n\nDataset schema:\n{schema_text}\n\n"
    if date_context:
        body += f"{date_context}\n\n"
    body += "Plan 3 KPIs and 4-6 charts for this dashboard."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": body}],
        system=PLANNER_PROMPT,
    )
    text = response.content[0].text
    text = re.sub(r"```json\s*|\s*```", "", text).strip()
    # Extract top-level JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Planner returned no valid JSON object")
    result = json.loads(match.group())
    return {
        "kpis":   result.get("kpis", []),
        "charts": result.get("charts", []),
    }


def stream_dashboard(request: str, workspace_id: str, dataset_id: str, schema: dict):
    """
    Generator — plans then streams KPIs and Plotly charts as SSE events.

    Events:
      {"type": "plan",        "kpis": [...titles], "charts": [...titles]}
      {"type": "kpi",         "index": i, "title": ..., "value": ..., "format": ...}
      {"type": "chart",       "index": i, "title": ..., "plotly": ...,
                               "insight": ..., "drill_dax_template": ..., "drill_next": ...}
      {"type": "error_chart", "index": i, "title": ..., "message": ...}
      {"type": "done"}
    """
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    yield {"type": "status", "message": "Checking dataset date range…"}
    date_context = _probe_date_range(workspace_id, dataset_id, schema)

    try:
        plan = plan_dashboard(request, schema, client, date_context)
    except Exception as e:
        yield {"type": "error", "message": f"Planning failed: {e}"}
        return

    kpi_specs   = plan.get("kpis", [])
    chart_specs = plan.get("charts", [])

    yield {
        "type":   "plan",
        "kpis":   [{"title": k["title"], "dax": k["dax"], "format": k.get("format", "number")}
                   for k in kpi_specs],
        "charts": [{"title": c["title"], "insight": c.get("insight", "")} for c in chart_specs],
    }

    # ── KPIs first ────────────────────────────────────────────────────────────
    for i, kpi in enumerate(kpi_specs):
        try:
            df = execute_dax(workspace_id, dataset_id, kpi["dax"])
            if df is None or df.empty:
                yield {"type": "kpi", "index": i, "title": kpi["title"],
                       "value": None, "format": kpi.get("format", "number")}
                continue
            raw = df.iloc[0, 0]
            value = float(raw) if pd.notna(raw) else 0.0
            yield {"type": "kpi", "index": i, "title": kpi["title"],
                   "value": value, "format": kpi.get("format", "number")}
        except Exception as e:
            yield {"type": "kpi", "index": i, "title": kpi["title"],
                   "value": None, "format": kpi.get("format", "number")}

    # ── Charts ────────────────────────────────────────────────────────────────
    for i, spec in enumerate(chart_specs):
        try:
            df = execute_dax(workspace_id, dataset_id, spec["dax"])
            if df is None or df.empty:
                yield {"type": "error_chart", "index": i,
                       "title": spec["title"], "message": "Query returned no data"}
                continue

            plotly_json = make_chart(df, spec, i)
            if not plotly_json:
                yield {"type": "error_chart", "index": i,
                       "title": spec["title"], "message": "Chart could not be rendered"}
                continue

            yield {
                "type":               "chart",
                "index":              i,
                "title":              spec["title"],
                "insight":            spec.get("insight", ""),
                "plotly":             plotly_json,
                "dax":                spec["dax"],
                "chart_type":         spec.get("chart_type", "bar"),
                "x_column":           spec.get("x_column", ""),
                "y_column":           spec.get("y_column", ""),
                "base_table":         spec.get("base_table", ""),
                "drill_dax_template": spec.get("drill_dax_template"),
                "drill_next":         spec.get("drill_next"),
            }

        except Exception as e:
            yield {"type": "error_chart", "index": i,
                   "title": spec.get("title", f"Chart {i+1}"), "message": str(e)}

    yield {"type": "done"}
