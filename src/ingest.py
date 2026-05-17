"""
Data ingest module — CSV/Excel upload, AI-assisted cleaning, Power BI Push Dataset.

Flow:
  1. upload_file()       → returns session_id + profile + transform suggestions
  2. apply_transform()   → mutates session df, returns new profile + preview
  3. push_to_powerbi()   → generator; creates Push Dataset schema then batches rows
"""

import io
import json
import os
import re
import uuid
from typing import Generator

import pandas as pd
from anthropic import Anthropic

from .auth import get_access_token

# ── Type mapping ──────────────────────────────────────────────────────────────
_PBI_TYPE: dict[str, str] = {
    "object":          "String",
    "string":          "String",
    "int64":           "Int64",
    "int32":           "Int64",
    "float64":         "Double",
    "float32":         "Double",
    "bool":            "Boolean",
    "datetime64[ns]":  "DateTime",
    "datetime64[us]":  "DateTime",
    "category":        "String",
}


def _pbi_type(dtype) -> str:
    return _PBI_TYPE.get(str(dtype), "String")


# ── In-memory session store ───────────────────────────────────────────────────
_sessions: dict[str, dict] = {}


def create_session(df: pd.DataFrame, filename: str) -> str:
    sid = str(uuid.uuid4())
    _sessions[sid] = {
        "df":         df.copy(),
        "original":   df.copy(),
        "filename":   filename,
        "transforms": [],
    }
    return sid


def get_session(sid: str) -> dict | None:
    return _sessions.get(sid)


def delete_session(sid: str) -> None:
    _sessions.pop(sid, None)


# ── Profiling ─────────────────────────────────────────────────────────────────
def profile_dataframe(df: pd.DataFrame) -> dict:
    """Return column-level quality profile."""
    total = len(df)
    cols = []
    for col in df.columns:
        s = df[col]
        null_count = int(s.isna().sum())
        dtype_str  = str(s.dtype)
        issues = []

        if null_count > 0:
            issues.append(f"{null_count} nulls ({null_count * 100 // max(total, 1)}%)")

        # Whitespace / mixed-case issues for string columns
        if s.dtype == object:
            ws = int((s.dropna().astype(str).str.strip() != s.dropna().astype(str)).sum())
            if ws > 0:
                issues.append(f"{ws} leading/trailing spaces")
            uniq = s.nunique()
            if uniq < 20:
                issues.append(f"{uniq} unique values")

        # Negative values in numeric columns where unexpected
        if pd.api.types.is_numeric_dtype(s):
            neg = int((s < 0).sum())
            if neg > 0 and col.lower() not in ("profit", "change", "diff", "delta", "balance"):
                issues.append(f"{neg} negative values")

        # Duplicate detection (only flag whole-df dupes on first column check)
        sample_vals = s.dropna().head(3).tolist()

        cols.append({
            "name":        col,
            "dtype":       dtype_str,
            "pbi_type":    _pbi_type(s.dtype),
            "null_count":  null_count,
            "null_pct":    round(null_count * 100 / max(total, 1), 1),
            "unique":      int(s.nunique()),
            "sample":      [str(v) for v in sample_vals],
            "issues":      issues,
        })

    dup_rows = int(df.duplicated().sum())
    return {
        "total_rows":  total,
        "total_cols":  len(df.columns),
        "dup_rows":    dup_rows,
        "columns":     cols,
    }


# ── AI transform suggestions ──────────────────────────────────────────────────
_SUGGEST_SYSTEM = """You are a data-cleaning expert. Given a pandas DataFrame profile,
suggest practical transformations to fix data quality issues.
Return ONLY a JSON array — no markdown, no extra text.

Each item:
{
  "id":     "unique_snake_case_id",
  "title":  "Short action title",
  "column": "column_name or null for whole-df ops",
  "reason": "one sentence why",
  "code":   "single Python expression or statement using 'df' variable"
}

Rules:
- code must use 'df' as the DataFrame variable
- single-line code only; use df = df[...] for filtering, df['col'] = ... for mutation
- for fill nulls: df['col'] = df['col'].fillna(0) or df['col'].fillna('Unknown')
- for strip whitespace: df['col'] = df['col'].str.strip()
- for title case: df['col'] = df['col'].str.title()
- for drop duplicates: df = df.drop_duplicates()
- for type cast: df['col'] = pd.to_datetime(df['col'], errors='coerce')
- Suggest at most 6 transformations. Only suggest what is actually needed based on the issues.
"""


def suggest_transformations(profile: dict, client: Anthropic) -> list[dict]:
    body = json.dumps(profile, indent=2)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        system=_SUGGEST_SYSTEM,
        messages=[{"role": "user", "content": f"Profile:\n{body}"}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"```json\s*|\s*```", "", text).strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    return json.loads(match.group())


# ── Apply a transformation ────────────────────────────────────────────────────
_SAFE_BUILTINS = {
    "__builtins__": {"print": print, "len": len, "range": range, "int": int,
                     "float": float, "str": str, "list": list, "dict": dict,
                     "set": set, "tuple": tuple, "bool": bool, "round": round},
}


def apply_transformation(df: pd.DataFrame, code: str) -> tuple[pd.DataFrame, str | None]:
    """
    Execute a single-line pandas transform in a restricted namespace.
    Returns (new_df, error_message).  error_message is None on success.
    """
    ns = {**_SAFE_BUILTINS, "df": df.copy(), "pd": pd}
    try:
        exec(compile(code, "<transform>", "exec"), ns)  # noqa: S102
        result = ns.get("df", df)
        if not isinstance(result, pd.DataFrame):
            return df, "Transform did not return a DataFrame"
        return result, None
    except Exception as e:
        return df, str(e)


# ── Power BI Push Dataset ─────────────────────────────────────────────────────
_BATCH_SIZE = 10_000


def _build_schema(df: pd.DataFrame, dataset_name: str) -> dict:
    """Build a Push Dataset schema JSON for Power BI REST API."""
    cols = [{"name": c, "dataType": _pbi_type(df[c].dtype)} for c in df.columns]
    return {
        "name": dataset_name,
        "tables": [{"name": dataset_name, "columns": cols}],
    }


def push_to_powerbi(
    df: pd.DataFrame,
    dataset_name: str,
    workspace_id: str,
) -> Generator[dict, None, None]:
    """
    Generator — creates a Push Dataset then streams rows in batches.

    Yields:
      {"type": "status",   "message": "..."}
      {"type": "progress", "done": n, "total": N}
      {"type": "done",     "dataset_id": "...", "dataset_name": "..."}
      {"type": "error",    "message": "..."}
    """
    import requests

    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    base = "https://api.powerbi.com/v1.0/myorg"

    # ── 1. Create dataset schema ──────────────────────────────────────────────
    yield {"type": "status", "message": f"Creating dataset '{dataset_name}' in Power BI…"}
    schema = _build_schema(df, dataset_name)
    url    = f"{base}/groups/{workspace_id}/datasets"
    r = requests.post(url, headers=headers, json=schema, timeout=30)
    if r.status_code not in (200, 201):
        yield {"type": "error", "message": f"Failed to create dataset: {r.text}"}
        return

    dataset_id = r.json().get("id", "")
    if not dataset_id:
        yield {"type": "error", "message": "Dataset created but ID not returned"}
        return
    yield {"type": "status", "message": f"Dataset created (id={dataset_id[:8]}…). Pushing rows…"}

    # ── 2. Push rows in batches ───────────────────────────────────────────────
    rows_url = f"{base}/groups/{workspace_id}/datasets/{dataset_id}/tables/{dataset_name}/rows"
    total    = len(df)
    pushed   = 0
    # Serialise: NaT → None, NaN → None
    records = json.loads(df.to_json(orient="records", date_format="iso", default_handler=str))

    for start in range(0, total, _BATCH_SIZE):
        batch = records[start : start + _BATCH_SIZE]
        r = requests.post(rows_url, headers=headers, json={"rows": batch}, timeout=60)
        if r.status_code not in (200, 201):
            yield {"type": "error", "message": f"Batch push failed at row {start}: {r.text}"}
            return
        pushed += len(batch)
        yield {"type": "progress", "done": pushed, "total": total}

    yield {"type": "done", "dataset_id": dataset_id, "dataset_name": dataset_name}
