"""
Power BI REST API client.

Wraps the Power BI REST API into clean Python methods.
Base URL: https://api.powerbi.com/v1.0/myorg

Key design: every method calls _get_token() which handles silent refresh
automatically — no need to manage token expiry anywhere else.

Schema discovery uses DAX Information Functions via executeQueries:
  - INFO.TABLES()   → all tables in the model
  - INFO.COLUMNS()  → all columns with data types
  - INFO.MEASURES() → all measures with their DAX expressions

This works for all dataset types (Import, DirectQuery, Composite).
"""

import requests
import pandas as pd
from .auth import get_access_token


BASE_URL = "https://api.powerbi.com/v1.0/myorg"


def _headers() -> dict:
    token = get_access_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_workspaces() -> list[dict]:
    """
    Return all workspaces the user has access to.

    The /groups endpoint returns shared workspaces only — it never includes
    'My Workspace' (the personal workspace). We prepend it manually using
    the sentinel id 'myorg', which list_datasets() and execute_dax() handle
    by hitting the /myorg/ endpoints directly instead of /groups/{id}/.
    """
    resp = requests.get(f"{BASE_URL}/groups", headers=_headers())
    resp.raise_for_status()
    workspaces = [{"id": w["id"], "name": w["name"]} for w in resp.json().get("value", [])]
    # Always prepend personal workspace
    return [{"id": "myorg", "name": "My Workspace"}] + workspaces


def list_datasets(workspace_id: str) -> list[dict]:
    """Return all datasets in a workspace. Handles personal workspace sentinel."""
    url = (
        f"{BASE_URL}/datasets"
        if workspace_id == "myorg"
        else f"{BASE_URL}/groups/{workspace_id}/datasets"
    )
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    datasets = resp.json().get("value", [])
    return [
        {"id": d["id"], "name": d["name"], "isRefreshable": d.get("isRefreshable", False)}
        for d in datasets
    ]


def list_reports(workspace_id: str) -> list[dict]:
    """Return all reports in a given workspace."""
    url = f"{BASE_URL}/groups/{workspace_id}/reports"
    resp = requests.get(url, headers=_headers())
    resp.raise_for_status()
    reports = resp.json().get("value", [])
    return [{"id": r["id"], "name": r["name"], "datasetId": r.get("datasetId")} for r in reports]


def get_dataset_schema(workspace_id: str, dataset_id: str) -> dict:
    """
    Return tables, columns, and measures for a dataset.

    Strategy (most to least capable):
    1. INFO DAX functions — works on Premium/PPU
    2. REST API /tables endpoint — works for push datasets
    3. Column discovery via TOPN(0) per table — works on all Pro+ datasets
    """
    # Try INFO functions first (Premium/PPU)
    try:
        tables_df  = execute_dax(workspace_id, dataset_id,
            "EVALUATE SELECTCOLUMNS(INFO.TABLES(), \"TableName\", [Name])")
        columns_df = execute_dax(workspace_id, dataset_id,
            """EVALUATE SELECTCOLUMNS(INFO.COLUMNS(),
               "TableName", [TableName], "ColumnName", [ExplicitName], "DataType", [DataType])""")
        measures_df = execute_dax(workspace_id, dataset_id,
            """EVALUATE SELECTCOLUMNS(INFO.MEASURES(),
               "TableName", [TableName], "MeasureName", [Name], "Expression", [Expression])""")
        return {
            "tables":   tables_df.to_dict("records")   if tables_df  is not None else [],
            "columns":  columns_df.to_dict("records")  if columns_df is not None else [],
            "measures": measures_df.to_dict("records") if measures_df is not None else [],
        }
    except Exception:
        pass  # fall through to REST API approach

    # Fallback: REST API tables endpoint (push datasets)
    tables = _get_tables_from_rest(workspace_id, dataset_id)

    if not tables:
        # Try common Power BI table names via TOPN(0) — works on all Pro datasets
        tables = _discover_tables_by_probing(workspace_id, dataset_id)

    if not tables:
        # No schema available — agent will explore via exploratory queries
        return {
            "tables": [], "columns": [], "measures": [],
            "needs_exploration": True,
            "note": "Schema unavailable. Start with exploratory DAX queries to discover tables.",
        }

    # Discover columns by fetching 1 row — column names come from row keys,
    # so TOPN(0) returns nothing useful. We fetch 1 row and discard the data.
    all_columns = []
    for t in tables:
        table_name = t["TableName"]
        try:
            df = execute_dax(workspace_id, dataset_id, f"EVALUATE TOPN(1, '{table_name}')")
            if df is not None and not df.empty:
                for col in df.columns:
                    all_columns.append({"TableName": table_name, "ColumnName": col, "DataType": "unknown"})
        except Exception:
            pass

    return {"tables": tables, "columns": all_columns, "measures": []}


def _discover_tables_by_probing(workspace_id: str, dataset_id: str) -> list[dict]:
    """
    Probe for table names by trying TOPN(0) against common Power BI table names.
    Returns any that succeed. Works on all Pro+ datasets.
    """
    candidates = [
        # Sample Superstore
        "Orders", "Returns", "People",
        # Common sales/retail names
        "Sales", "Products", "Customers", "Dates", "Date", "Calendar",
        "Regions", "Categories", "Employees", "Stores", "Targets",
        # Common HR/finance names
        "Budget", "Actuals", "Transactions", "Invoices", "Accounts",
        "Fact Sales", "Dim Customer", "Dim Product", "Dim Date",
        "FactSales", "DimCustomer", "DimProduct", "DimDate",
    ]
    found = []
    for name in candidates:
        try:
            df = execute_dax(workspace_id, dataset_id, f"EVALUATE TOPN(1, '{name}')")
            if df is not None:
                found.append({"TableName": name})
        except Exception:
            pass
    return found


def _get_tables_from_rest(workspace_id: str, dataset_id: str) -> list[dict]:
    """Use REST API to list tables — works for push datasets."""
    url = (
        f"{BASE_URL}/datasets/{dataset_id}/tables"
        if workspace_id == "myorg"
        else f"{BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/tables"
    )
    try:
        resp = requests.get(url, headers=_headers())
        if resp.ok:
            return [{"TableName": t["name"]} for t in resp.json().get("value", [])]
    except Exception:
        pass
    return []


def execute_dax(workspace_id: str, dataset_id: str, dax_query: str) -> pd.DataFrame | None:
    """
    Execute a DAX query and return results as a DataFrame.
    Handles personal workspace sentinel id 'myorg'.
    """
    url = (
        f"{BASE_URL}/datasets/{dataset_id}/executeQueries"
        if workspace_id == "myorg"
        else f"{BASE_URL}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    )
    payload = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }

    resp = requests.post(url, json=payload, headers=_headers())

    if not resp.ok:
        error_msg = resp.json().get("error", {}).get("message", resp.text)
        raise ValueError(f"DAX execution failed: {error_msg}")

    results  = resp.json()
    table    = results["results"][0]["tables"][0]
    rows     = table.get("rows", [])
    api_cols = table.get("columns", [])  # present even when rows is empty

    if not rows:
        # Build an empty DataFrame with the correct column names
        if api_cols:
            col_names = [c["name"].split("[")[-1].rstrip("]") for c in api_cols]
            return pd.DataFrame(columns=col_names)
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Strip "TableName[ColumnName]" → "ColumnName"
    df.columns = [c.split("[")[-1].rstrip("]") for c in df.columns]
    return df
