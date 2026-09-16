"""Jarvis Control Room API -- live view over the pipeline that already exists (bronze/silver/
gold via control.run_registry), the agentic SDLC's real evidence trail, and a human-checkpointed
catalogue upload flow that runs the existing compilers. No pipeline logic lives here: every
route composes emitters/ and harness/ modules that already work and are already tested
(tests/test_pipeline.py) -- this is a UI over them, not a second implementation.

Run:  python -m uvicorn webapp.backend.main:app --reload --port 8010   (from the repo root)
"""
from __future__ import annotations

import base64
import pathlib
import secrets
import sys

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# GOOGLE_API_KEY etc. -- needed by agent_runs._inspect_pending, which has to construct a (never
# invoked) model instance just to reconnect to a paused graph's state. Every other entry point
# in this project loads .env the same way (bronze_loader.py, daily_digest.py, run_cli.py, ...);
# the webapp process is the one place that hadn't, until GET /api/agent-runs/<id> 500'd on it.
import os  # noqa: E402
_env_path = REPO_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from webapp.backend import agent_runs, journey, pipeline, sdlc, uploads  # noqa: E402

app = FastAPI(title="Jarvis Control Room")

FRONTEND_DIR = REPO_ROOT / "webapp" / "frontend"

# -- access control -----------------------------------------------------------------
# This app can trigger real agent runs (real Gemini calls) and approve real contract writes,
# so it must not sit open on the public internet with no login. Both env vars unset (the
# local-dev default) leaves it open, matching every prior session's behavior; set both to
# require HTTP Basic auth on every request, which is what the deployed instance does.
_CONTROL_ROOM_USER = os.environ.get("CONTROL_ROOM_USER")
_CONTROL_ROOM_PASSWORD = os.environ.get("CONTROL_ROOM_PASSWORD")
_CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]


@app.middleware("http")
async def _require_basic_auth(request: Request, call_next):
    # CORS preflight carries no credentials by design -- let CORSMiddleware (added below,
    # so it wraps this middleware) answer it before auth is ever checked. The page shell
    # itself (/, /static/*) stays open too: gating it would make the *browser's own* native
    # Basic Auth dialog block the top-level navigation before our page's JS ever runs, which
    # pre-empts the in-page login form below and can't be scripted against. Only /api/* -- the
    # routes that actually read data or trigger real agent runs -- need a login.
    if (
        request.method == "OPTIONS"
        or not (_CONTROL_ROOM_USER and _CONTROL_ROOM_PASSWORD)
        or not request.url.path.startswith("/api/")
    ):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    ok = False
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
            ok = secrets.compare_digest(user, _CONTROL_ROOM_USER) and secrets.compare_digest(
                pwd, _CONTROL_ROOM_PASSWORD
            )
        except Exception:  # noqa: BLE001 -- any malformed header just fails auth, doesn't 500
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Jarvis Control Room"'})
    return await call_next(request)


if _CORS_ORIGINS:
    # Added after _require_basic_auth so it ends up outermost (Starlette wraps middleware in
    # reverse registration order) -- CORS headers, including on a 401, must reach the browser
    # or a cross-origin frontend (e.g. the Vercel copy of this UI) sees an opaque network error
    # instead of a real 401 it can react to.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/api/targets")
def api_targets():
    return {"targets": pipeline.available_targets()}


@app.get("/api/domains")
def api_domains():
    """Every domain that actually has compiled contracts on disk -- the Control Room's domain
    selector reads this instead of a hardcoded list, so a domain onboarded through the agent
    Freeze gate (Phase E) shows up here with no code change."""
    sources_dir = pipeline.REPO_ROOT / "contracts" / "sources"
    domains = sorted(p.name for p in sources_dir.iterdir() if p.is_dir()) if sources_dir.exists() else []
    return {"domains": domains or [pipeline.DEFAULT_DOMAIN]}


@app.get("/api/pipeline/flow")
def api_pipeline_flow(target: str = "duckdb", domain: str = pipeline.DEFAULT_DOMAIN):
    try:
        return pipeline.pipeline_flow(target, domain)
    except Exception as exc:  # noqa: BLE001 -- surface the real error to the UI, don't swallow it
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/alerts")
def api_alerts(target: str = "duckdb", domain: str = pipeline.DEFAULT_DOMAIN):
    try:
        return pipeline.recent_alerts(target, domain=domain)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/journey")
def api_journey(domain: str = pipeline.DEFAULT_DOMAIN, target: str = "duckdb"):
    """The five-step journey plus where this domain actually stands in it. Renders from
    agents/gates.py, the same registry that decides what interrupts the agent graph."""
    try:
        return journey.journey(domain, target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sdlc")
def api_sdlc():
    return sdlc.stage_status()


@app.get("/api/uploads")
def api_uploads_list():
    return {"uploads": uploads.list_uploads()}


@app.post("/api/uploads")
async def api_uploads_create(kind: str = Form(...), file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="only .xlsx workbooks are accepted")
    content = await file.read()
    try:
        return uploads.save_and_preview(kind, file.filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/uploads/{upload_id}/approve")
def api_uploads_approve(upload_id: str):
    try:
        return uploads.approve_and_compile(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/agent-runs")
def api_agent_runs_list():
    return {"runs": agent_runs.list_runs()}


@app.get("/api/agent-runs/{run_id}")
def api_agent_runs_detail(run_id: str):
    try:
        return agent_runs.get_run_detail(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/agent-runs")
def api_agent_runs_start(upload_id: str = Form(...), domain_hint: str = Form("unknown")):
    """Starts a real agent run against an already-uploaded workbook (see /api/uploads) -- the
    agent previews it, and if it looks reasonable, attempts to Freeze it, which pauses at the
    real gate proven in evidence/runs/phase-c-agent-activation.md for a human to approve here."""
    record = next((u for u in uploads.list_uploads() if u["id"] == upload_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no upload {upload_id!r}")
    return agent_runs.start_run(record["stored_path"], domain_hint)


@app.post("/api/agent-runs/{run_id}/approve")
def api_agent_runs_approve(run_id: str):
    try:
        return agent_runs.resume_run(run_id, "approve")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/agent-runs/{run_id}/reject")
def api_agent_runs_reject(run_id: str, message: str = Form("")):
    try:
        return agent_runs.resume_run(run_id, "reject", message or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
