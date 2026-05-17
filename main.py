"""
FastAPI entry point for the Power BI Analyst Agent.

Run with:  uvicorn main:app --reload --port 8000
Then open: http://localhost:8000
"""

import json
import os
import io
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from anthropic import Anthropic
from src.auth import get_access_token, start_device_flow, poll_device_flow, is_authenticated
from src.client import list_workspaces, list_datasets, get_dataset_schema, execute_dax
from src.agent import stream_analysis
from src.dashboard import stream_dashboard, make_chart, inject_filter
from src.state import AgentState
from src.ingest import (
    create_session, get_session, delete_session,
    profile_dataframe, suggest_transformations,
    apply_transformation, push_to_powerbi,
)

load_dotenv()

app = FastAPI(title="Power BI Analyst Agent")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Serve the SPA ──────────────────────────────────────────────────────────────
@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")


# ── Auth ───────────────────────────────────────────────────────────────────────
@app.get("/api/auth/status")
async def auth_status():
    """Check if a valid cached token exists."""
    return {"authenticated": is_authenticated()}


@app.post("/api/auth/start")
async def auth_start():
    """
    Stage 1 — initiate device flow, return login code immediately.
    The browser shows the code + link without waiting.
    """
    try:
        flow = start_device_flow()
        return {"status": "ok", **flow}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/auth/poll")
async def auth_poll():
    """
    Stage 2 — SSE endpoint that streams progress while waiting for login.
    Sends 'waiting' ticks every 5 s, then 'done' when the token arrives.
    """
    def generator():
        import time
        # Send a heartbeat immediately so the browser knows the connection is live
        yield 'data: {"type":"waiting","message":"Waiting for login..."}\n\n'
        try:
            poll_device_flow()          # blocks until user completes login
            yield 'data: {"type":"done"}\n\n'
        except Exception as e:
            yield f'data: {{"type":"error","message":"{str(e)}"}}\n\n'

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Workspace & dataset discovery ──────────────────────────────────────────────
@app.get("/api/workspaces")
async def get_workspaces():
    try:
        return {"workspaces": list_workspaces()}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/workspaces/{workspace_id}/datasets")
async def get_datasets(workspace_id: str):
    try:
        return {"datasets": list_datasets(workspace_id)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/workspaces/{workspace_id}/datasets/{dataset_id}/schema")
async def get_schema(workspace_id: str, dataset_id: str):
    try:
        schema = get_dataset_schema(workspace_id, dataset_id)
        return {"schema": schema}
    except Exception as e:
        return {"error": str(e)}


# ── Analysis stream ────────────────────────────────────────────────────────────
@app.get("/api/analyse")
async def analyse(
    workspace_id: str = Query(...),
    workspace_name: str = Query(...),
    dataset_id: str = Query(...),
    dataset_name: str = Query(...),
    question: str = Query(...),
):
    """
    SSE endpoint — streams agent events as the analysis runs.

    Server-Sent Events format:
      data: {"type": "thinking", "thought": "..."}\n\n
      data: {"type": "dax", "query": "..."}\n\n
      ...

    The browser's EventSource API reads these and renders each event.
    """

    def event_generator():
        schema = get_dataset_schema(workspace_id, dataset_id)
        state = AgentState(
            question=question,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
        )
        for event in stream_analysis(state, schema):
            # SSE format: "data: <json>\n\n"
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind a proxy
        },
    )


# ── Dashboard stream ───────────────────────────────────────────────────────────
@app.get("/api/dashboard")
async def dashboard(
    workspace_id: str = Query(...),
    workspace_name: str = Query(...),
    dataset_id: str = Query(...),
    dataset_name: str = Query(...),
    request: str = Query(...),
):
    """
    SSE endpoint — plans 4-6 charts, then streams each as it completes.

    Events:
      {"type": "plan",       "charts": [{"title": ..., "insight": ...}, ...]}
      {"type": "chart",      "index": 0, "title": ..., "image": "<b64>", "insight": ...}
      {"type": "error_chart","index": 0, "title": ..., "message": ...}
      {"type": "done"}
    """

    def event_generator():
        schema = get_dataset_schema(workspace_id, dataset_id)
        for event in stream_dashboard(request, workspace_id, dataset_id, schema):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Drill-down ─────────────────────────────────────────────────────────────────
@app.get("/api/drilldown")
async def drilldown(
    workspace_id: str = Query(...),
    dataset_id: str = Query(...),
    dax: str = Query(...),
    title: str = Query("Drill-down"),
):
    """
    Execute a drill-down DAX query and return a Plotly chart JSON.
    Called when the user clicks a bar or pie slice in the dashboard.
    """
    try:
        df = execute_dax(workspace_id, dataset_id, dax)
        if df is None or df.empty:
            return {"error": "No data returned for this filter"}
        spec = {
            "x_column": df.columns[0],
            "y_column": df.columns[-1],
            "chart_type": "bar",
        }
        plotly_json = make_chart(df, spec, 0)
        if not plotly_json:
            return {"error": "Chart could not be rendered"}
        return {"plotly": plotly_json}
    except Exception as e:
        return {"error": str(e)}


# ── Cross-filter refresh ───────────────────────────────────────────────────────
@app.get("/api/filter-chart")
async def filter_chart(
    workspace_id: str = Query(...),
    dataset_id:   str = Query(...),
    dax:          str = Query(...),
    chart_type:   str = Query("bar"),
    x_column:     str = Query(...),
    y_column:     str = Query(...),
    index:        int = Query(0),
    filters:      str = Query("[]"),   # JSON array of {table, col, val}
):
    """Re-execute a chart's DAX with cross-filters injected, return new Plotly JSON."""
    try:
        current_dax = dax
        for f in json.loads(filters):
            current_dax = inject_filter(current_dax, f["table"], f["col"], f["val"])
        df = execute_dax(workspace_id, dataset_id, current_dax)
        if df is None or df.empty:
            return {"error": "No data for this filter combination"}
        spec = {"x_column": x_column, "y_column": y_column, "chart_type": chart_type}
        plotly_json = make_chart(df, spec, index)
        return {"plotly": plotly_json} if plotly_json else {"error": "Chart could not be rendered"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/filter-kpi")
async def filter_kpi(
    workspace_id: str = Query(...),
    dataset_id:   str = Query(...),
    dax:          str = Query(...),
    filters:      str = Query("[]"),
):
    """Re-execute a KPI DAX with cross-filters injected, return the numeric value."""
    try:
        current_dax = dax
        for f in json.loads(filters):
            current_dax = inject_filter(current_dax, f["table"], f["col"], f["val"])
        df = execute_dax(workspace_id, dataset_id, current_dax)
        if df is None or df.empty:
            return {"value": None}
        raw = df.iloc[0, 0]
        return {"value": float(raw) if pd.notna(raw) else 0.0}
    except Exception as e:
        return {"error": str(e), "value": None}


# ── Data ingest ────────────────────────────────────────────────────────────────
@app.post("/api/ingest/upload")
async def ingest_upload(file: UploadFile = File(...)):
    """
    Accept a CSV or Excel file, profile it, and return AI transform suggestions.
    Returns: {session_id, filename, profile, suggestions}
    """
    try:
        contents = await file.read()
        filename = file.filename or "upload"
        if filename.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            df = pd.read_csv(io.BytesIO(contents))

        session_id = create_session(df, filename)
        profile    = profile_dataframe(df)

        client      = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        suggestions = suggest_transformations(profile, client)

        return {
            "session_id":  session_id,
            "filename":    filename,
            "profile":     profile,
            "suggestions": suggestions,
            "preview":     df.head(10).to_dict(orient="records"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ingest/transform")
async def ingest_transform(
    session_id: str = Form(...),
    code:       str = Form(...),
):
    """Apply a single pandas transformation to the session DataFrame."""
    session = get_session(session_id)
    if not session:
        return {"error": "Session not found or expired"}
    try:
        new_df, err = apply_transformation(session["df"], code)
        if err:
            return {"error": err}
        session["df"] = new_df
        session["transforms"].append(code)
        profile  = profile_dataframe(new_df)
        preview  = new_df.head(10).to_dict(orient="records")
        return {"ok": True, "profile": profile, "preview": preview}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ingest/push")
async def ingest_push(
    session_id:   str = Query(...),
    workspace_id: str = Query(...),
    dataset_name: str = Query(...),
):
    """
    SSE endpoint — push session DataFrame to Power BI as a Push Dataset.
    Streams progress events until done or error.
    """
    session = get_session(session_id)
    if not session:
        async def _err():
            yield 'data: {"type":"error","message":"Session not found"}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def event_generator():
        for event in push_to_powerbi(session["df"], dataset_name, workspace_id):
            yield f"data: {json.dumps(event)}\n\n"
        delete_session(session_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
