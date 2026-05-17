# Power BI Analyst Agent

An AI-powered web application that connects to Power BI datasets and lets you ask business questions in plain English, generate interactive dashboards, and upload/clean data — all without writing DAX or SQL.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green)
![Claude](https://img.shields.io/badge/Claude-Sonnet%20%2F%20Haiku-orange)

---

## Features

### Analyse mode
Ask a business question in plain English. The agent writes DAX queries, executes them against your Power BI dataset, and streams its reasoning step-by-step until it reaches a data-backed answer.

- ReAct agent loop (think → write DAX → execute → observe → repeat)
- Multi-model: Claude Haiku for DAX generation, Sonnet for synthesis
- Iteration guard with urgency injection when steps are running low
- Results rendered as formatted markdown with tables

### Dashboard mode
Describe what you want to visualise. The agent plans 3 KPI cards and 4–6 charts, then builds each one in parallel.

- KPI summary cards (currency / count / percent formatting)
- Interactive Plotly charts (bar, line, area, donut pie)
- Cross-filtering: click any bar or pie slice to filter all other charts
- Per-card actions: Refresh, Expand, Download CSV, Drill-down
- Date-range probing: automatically detects actual data range to avoid empty queries
- Drill-down modal for next-level breakdowns

### Data Upload
Upload a CSV or Excel file, get an AI quality audit, apply one-click fixes, and push the cleaned dataset directly to Power BI as a Push Dataset.

- Column-level profiling: nulls, whitespace, unexpected negatives, duplicates
- Claude Haiku suggests up to 6 targeted pandas transforms
- Live preview updates after each transform is applied
- Pushes to Power BI REST API in 10,000-row batches with a progress bar

---

## Architecture

```
Browser (SSE + Fetch)
       │
FastAPI (main.py)  ←─── Server-Sent Events for streaming
       │
  ┌────┴────┐
  │         │
src/        src/
agent.py    dashboard.py    ← ReAct loop / Dashboard planner
auth.py     ingest.py       ← MSAL device flow / CSV upload + push
client.py   state.py        ← Power BI REST API / AgentState
```

**Auth**: MSAL device code flow — the browser shows a one-time code; the user visits microsoft.com/devicelogin, and the token is cached locally.

**Streaming**: All long-running operations (analysis, dashboard build, data push) use FastAPI `StreamingResponse` with Server-Sent Events so the browser updates in real time.

---

## Quick start

### 1. Prerequisites

- Python 3.11+
- A Power BI Pro or Premium Per User licence
- An [Anthropic API key](https://console.anthropic.com)
- An Azure AD App Registration (see below)

### 2. Azure AD App Registration

1. Go to **Azure Portal → App registrations → New registration**
2. Name it (e.g. `pbi-analyst-agent`), leave redirect URI blank
3. Under **API permissions**, add:
   - `Power BI Service → Dataset.ReadWrite.All`
   - `Power BI Service → Workspace.Read.All`
4. Grant admin consent
5. Copy the **Application (client) ID** and **Directory (tenant) ID**

### 3. Install & configure

```bash
git clone https://github.com/<your-username>/powerbi-analyst-agent.git
cd powerbi-analyst-agent

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# Configure secrets
cp .env.example .env
# Edit .env with your keys
```

### 4. Run

```bash
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000), click **Connect to Power BI**, complete the device login, then select a workspace and dataset.

---

## Project structure

```
powerbi-analyst-agent/
├── main.py               # FastAPI app + all API endpoints
├── requirements.txt
├── .env.example
├── src/
│   ├── agent.py          # ReAct agent loop, DAX tool, streaming
│   ├── auth.py           # MSAL device code flow, token cache
│   ├── client.py         # Power BI REST API client
│   ├── dashboard.py      # Dashboard planner, Plotly chart builder
│   ├── ingest.py         # File upload, profiling, AI suggestions, push
│   └── state.py          # AgentState dataclass
└── static/
    ├── index.html
    ├── css/style.css
    └── js/app.js
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, Python 3.11 |
| AI | Anthropic Claude (Sonnet 4.6 + Haiku 4.5) |
| Auth | MSAL (Microsoft Authentication Library) |
| Data | pandas, openpyxl |
| Charts | Plotly.js |
| Power BI | REST API (DAX execution, Push Datasets) |
| Streaming | Server-Sent Events (SSE) |

---

## Limitations

- **Push Datasets only**: the data upload feature creates a new Push Dataset; it cannot update existing Import-mode datasets (that requires the Power BI dataflow / Fabric pipeline API)
- **Single-user**: the in-memory session store is not suitable for multi-user deployments — replace with Redis for production
- **No auth middleware**: the FastAPI app has no user authentication; run behind a VPN or add OAuth2 middleware before exposing publicly
- **Rate limits**: the Anthropic API and Power BI REST API both have rate limits that are not currently handled with retry/backoff logic
