"""Jarvis Control Room API -- live view over the pipeline that already exists (bronze/silver/
gold via control.run_registry), the agentic SDLC's real evidence trail, and a human-checkpointed
catalogue upload flow that runs the existing compilers. No pipeline logic lives here: every
route composes emitters/ and harness/ modules that already work and are already tested
(tests/test_pipeline.py) -- this is a UI over them, not a second implementation.

Run:  python -m uvicorn webapp.backend.main:app --reload --port 8010   (from the repo root)
"""
from __future__ import annotations

import base64
import json
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

from webapp.backend import agent_runs, architecture, dashboard, discovery, intent, journey, ops_tickets, pipeline, sdlc, test_pack, uploads  # noqa: E402
from pydantic import BaseModel  # noqa: E402

app = FastAPI(title="Jarvis Control Room")

FRONTEND_DIR = REPO_ROOT / "webapp" / "frontend"

# -- access control -----------------------------------------------------------------
# This app can trigger real agent runs (real Gemini calls) and approve real contract writes,
# so it must not sit open on the public internet with no login, and a multi-tenant deployment
# (two companies plus an admin sharing one instance) needs logins that are actually scoped to a
# domain -- not just a UI that hides the switcher. CONTROL_ROOM_USERS is a JSON array of
# {"username", "password", "domains", "label"}; "domains" is either a list (e.g. ["insurance"])
# or the literal "*" for an account that sees every domain, same as today's admin. Falls back to
# the single CONTROL_ROOM_USER/CONTROL_ROOM_PASSWORD pair (unchanged shape, "*" access) if
# CONTROL_ROOM_USERS isn't set, so an existing single-tenant deployment needs no env change.
# Neither set (the local-dev default) leaves the API open, matching every prior session.
def _load_users() -> list[dict]:
    raw = os.environ.get("CONTROL_ROOM_USERS")
    if raw:
        try:
            users = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 -- fail loudly at boot, not silently open
            raise RuntimeError(f"CONTROL_ROOM_USERS is not valid JSON: {exc}") from exc
        for u in users:
            if not ({"username", "password", "domains"} <= u.keys()):
                raise RuntimeError(f"CONTROL_ROOM_USERS entry missing username/password/domains: {u}")
        return users
    single_user = os.environ.get("CONTROL_ROOM_USER")
    single_pwd = os.environ.get("CONTROL_ROOM_PASSWORD")
    if single_user and single_pwd:
        return [{"username": single_user, "password": single_pwd, "domains": "*", "label": "Admin"}]
    return []


_USERS = _load_users()
_CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]


def _find_user(username: str, password: str) -> dict | None:
    for u in _USERS:
        if secrets.compare_digest(u["username"], username) and secrets.compare_digest(u["password"], password):
            return u
    return None


@app.middleware("http")
async def _require_basic_auth(request: Request, call_next):
    # CORS preflight carries no credentials by design -- let CORSMiddleware (added below,
    # so it wraps this middleware) answer it before auth is ever checked. The page shell
    # itself (/, /static/*) stays open too: gating it would make the *browser's own* native
    # Basic Auth dialog block the top-level navigation before our page's JS ever runs, which
    # pre-empts the in-page login form below and can't be scripted against. Only /api/* -- the
    # routes that actually read data or trigger real agent runs -- need a login.
    if request.method == "OPTIONS" or not _USERS or not request.url.path.startswith("/api/"):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    user = None
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            uname, _, pwd = decoded.partition(":")
            user = _find_user(uname, pwd)
        except Exception:  # noqa: BLE001 -- any malformed header just fails auth, doesn't 500
            user = None
    if user is None:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Jarvis Control Room"'})
    request.state.user = user
    return await call_next(request)


def _user_domains(request: Request) -> list[str] | str:
    """"*" (every domain, admin) or the exact list this logged-in account may see. When auth is
    off (no _USERS configured, local dev) everything is visible, matching pre-multi-tenant
    behavior."""
    if not _USERS:
        return "*"
    user = getattr(request.state, "user", None)
    return user["domains"] if user else []


def _check_domain(request: Request, domain: str) -> None:
    """Real enforcement, not just a UI convenience: called at the top of every route that reads
    or writes one domain's data, so a Star Insurance login physically cannot pull asset_management
    data by editing the URL, even though the frontend would never construct that URL itself."""
    allowed = _user_domains(request)
    if allowed != "*" and domain not in allowed:
        raise HTTPException(status_code=403, detail=f"your account does not have access to domain {domain!r}")


def _require_admin(request: Request) -> None:
    """Onboarding a brand-new domain (an intake workbook upload) has no domain to scope against
    yet -- that's the whole point of intake -- so it's restricted to the "*" (admin) account
    instead, rather than left open to any logged-in company."""
    if _user_domains(request) != "*":
        raise HTTPException(status_code=403, detail="only an admin account can do this")


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


@app.get("/api/me")
def api_me(request: Request):
    """Who's logged in and what they can see -- the frontend calls this right after login to
    build the domain switcher instead of assuming every account sees every domain."""
    user = getattr(request.state, "user", None)
    if not _USERS:
        return {"username": None, "label": "Local (no auth)", "domains": "*"}
    return {"username": user["username"], "label": user.get("label", user["username"]), "domains": user["domains"]}


@app.get("/api/domains")
def api_domains(request: Request):
    """Every domain that actually has compiled contracts on disk AND that this logged-in
    account is allowed to see -- the Control Room's domain selector reads this instead of a
    hardcoded list, so a domain onboarded through the agent Freeze gate (Phase E) shows up here
    with no code change, and a scoped company login never even sees another domain's name."""
    sources_dir = pipeline.REPO_ROOT / "contracts" / "sources"
    domains = sorted(p.name for p in sources_dir.iterdir() if p.is_dir()) if sources_dir.exists() else []
    domains = domains or [pipeline.DEFAULT_DOMAIN]
    allowed = _user_domains(request)
    if allowed != "*":
        domains = [d for d in domains if d in allowed]
    return {"domains": domains}


@app.get("/api/pipeline/flow")
def api_pipeline_flow(request: Request, target: str = "duckdb", domain: str = pipeline.DEFAULT_DOMAIN):
    _check_domain(request, domain)
    try:
        return pipeline.pipeline_flow(target, domain)
    except Exception as exc:  # noqa: BLE001 -- surface the real error to the UI, don't swallow it
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/alerts")
def api_alerts(request: Request, target: str = "duckdb", domain: str = pipeline.DEFAULT_DOMAIN):
    _check_domain(request, domain)
    try:
        return pipeline.recent_alerts(target, domain=domain)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class ConnectionTestRequest(BaseModel):
    connection: dict


class ProfileRequest(BaseModel):
    connection: dict
    source_id: str
    domain: str
    sample_limit: int = 500


@app.post("/api/discovery/test")
def api_discovery_test(body: ConnectionTestRequest):
    """Cheap reachability check for a candidate connection -- no sampling. Same connection
    shape a compiled source.yaml's connection: block uses."""
    return discovery.test_connection(body.connection)


@app.post("/api/discovery/profile")
def api_discovery_profile(request: Request, body: ProfileRequest):
    """Connects for real and profiles a candidate source: per-column type/null/distinct/
    candidate-key, persisted to contracts/discovery/<domain>/<source_id>.profile.json."""
    _check_domain(request, body.domain)
    try:
        return discovery.profile_source(body.connection, body.source_id, body.domain, body.sample_limit)
    except Exception as exc:  # noqa: BLE001 -- surface the real connector error, don't swallow it
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/discovery")
def api_discovery_list(request: Request, domain: str = pipeline.DEFAULT_DOMAIN):
    _check_domain(request, domain)
    return {"domain": domain, "profiles": discovery.list_profiles(domain)}


class IntentRequest(BaseModel):
    domain: str
    client: str = "default"
    intent: dict
    captured_by: str = "human"
    intent_id: str | None = None


@app.get("/api/intent")
def api_intent_get(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, intent_id: str | None = None):
    _check_domain(request, domain)
    return {"domain": domain, "intent": intent.load_intent(domain, intent_id)}


@app.get("/api/intents")
def api_intents_list(request: Request, domain: str = pipeline.DEFAULT_DOMAIN):
    """Every intent this domain has captured -- a real deployment usually serves more than one
    application off the same domain (a claims dashboard AND a regulatory extract, say), each
    with its own report list and SLA."""
    _check_domain(request, domain)
    return {"domain": domain, "intents": intent.list_intents(domain)}


@app.post("/api/intent")
def api_intent_post(request: Request, body: IntentRequest):
    _check_domain(request, body.domain)
    return intent.capture_intent(body.domain, body.client, body.intent, body.captured_by, body.intent_id)


@app.delete("/api/intent/{domain}/{intent_id}")
def api_intent_delete(request: Request, domain: str, intent_id: str):
    _check_domain(request, domain)
    return intent.delete_intent(domain, intent_id)


class CatalogueChatRequest(BaseModel):
    kind: str            # "intent" | "architecture"
    domain: str
    history: list[dict]  # [{"role": "user"|"assistant", "text": str}, ...]
    message: str


@app.post("/api/catalogue/chat")
def api_catalogue_chat(request: Request, body: CatalogueChatRequest):
    """A real conversation with the BA (intent) or SA (architecture) agent, instead of typing
    every field into the form directly -- see emitters/catalogue_chat.py. Nothing here writes to
    contracts/; a structured suggestion (when the agent has enough to draft one) is only ever
    applied to the unsaved form on the frontend."""
    _check_domain(request, body.domain)
    try:
        from emitters.catalogue_chat import chat_turn
        return chat_turn(body.kind, body.domain, body.history, body.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- surface the real error (bad key, model error, ...)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/memory")
def api_memory_list(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, agent: str | None = None,
                    target: str = "duckdb"):
    """What an agent has actually remembered for this domain -- never a black box, see
    emitters/agent_memory.py."""
    _check_domain(request, domain)
    from emitters.agent_memory import list_memory
    return {"domain": domain, "memories": list_memory(domain, agent, target)}


@app.delete("/api/memory/{domain}/{memory_id}")
def api_memory_forget(request: Request, domain: str, memory_id: int, target: str = "duckdb"):
    _check_domain(request, domain)
    from emitters.agent_memory import forget
    return {"ok": forget(domain, memory_id, target)}


@app.get("/api/intent/gap-analysis")
def api_gap_analysis(request: Request, domain: str = pipeline.DEFAULT_DOMAIN):
    _check_domain(request, domain)
    return intent.run_gap_analysis(domain)


class ArchitectureRequest(BaseModel):
    domain: str
    client: str = "default"
    architecture: dict
    captured_by: str = "human"


class ArchitectureCheckRequest(BaseModel):
    domain: str
    workbook_path: str


@app.get("/api/architecture")
def api_architecture_get(request: Request, domain: str = pipeline.DEFAULT_DOMAIN):
    _check_domain(request, domain)
    return {"domain": domain, "architecture": architecture.load_architecture(domain)}


@app.post("/api/architecture")
def api_architecture_post(request: Request, body: ArchitectureRequest):
    _check_domain(request, body.domain)
    return architecture.capture_architecture(body.domain, body.client, body.architecture, body.captured_by)


@app.post("/api/architecture/check")
def api_architecture_check(request: Request, body: ArchitectureCheckRequest):
    _check_domain(request, body.domain)
    result = architecture.check_consistency(body.domain, body.workbook_path)
    if not result.get("ok", True) and "errors" in result:
        raise HTTPException(status_code=400, detail=result["errors"])
    return result


@app.get("/api/architecture/diagram")
def api_architecture_diagram(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, level: str = "high"):
    """Auto-generated from the domain's own real contracts, never hand-drawn -- level=high is
    source->bronze->silver->gold; level=low is the silver dimensions/facts with their real
    columns and declared foreign keys. See emitters/diagram.py."""
    _check_domain(request, domain)
    from emitters.diagram import high_level_diagram, low_level_diagram
    return high_level_diagram(domain) if level == "high" else low_level_diagram(domain)


@app.post("/api/architecture/interpret-image")
async def api_architecture_interpret_image(request: Request, domain: str = Form(...), file: UploadFile = File(...)):
    """A real Gemini vision call reads an uploaded architecture sketch/photo and proposes a
    structured draft -- never written to contracts/ here. The Control Room only applies it to
    the (unsaved) form; a human still has to click Save."""
    _check_domain(request, domain)
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="only image files are accepted")
    content = await file.read()
    try:
        return architecture.interpret_architecture_image(content, file.content_type)
    except Exception as exc:  # noqa: BLE001 -- surface the real error (bad key, unreadable image, ...)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/test-pack")
def api_test_pack(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, target: str = "duckdb"):
    """Runs the real per-layer (3A/3B/3C) test pack for this domain -- see
    emitters/test_pack.py. Every case is derived from the domain's own contracts."""
    _check_domain(request, domain)
    try:
        return test_pack.run(domain, target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class DashboardSaveRequest(BaseModel):
    domain: str
    name: str
    tiles: list[dict]


@app.get("/api/dashboard")
def api_dashboard(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, target: str = "duckdb"):
    """Renders Step 04's dashboard for this domain against LIVE gold data -- whatever's already
    saved in the gold contract, or a fresh proposal (mechanically derived from the domain's own
    metrics/marts) if none exists yet. See emitters/dashboard.py."""
    _check_domain(request, domain)
    try:
        return dashboard.render(domain, target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/dashboard")
def api_dashboard_save(request: Request, body: DashboardSaveRequest):
    """Saves an accepted (or edited) proposal into contracts/semantics/<domain>.gold.yaml's
    dashboards: field -- a field the schema has always had, now actually populated."""
    _check_domain(request, body.domain)
    return dashboard.save(body.domain, body.name, body.tiles)


class TicketAssignRequest(BaseModel):
    domain: str
    target: str = "duckdb"
    ticket_id: int
    assigned_to: str


class TicketFixRequest(BaseModel):
    domain: str
    target: str = "duckdb"
    ticket_id: int
    resolution_note: str


class TicketRejectRequest(BaseModel):
    domain: str
    target: str = "duckdb"
    ticket_id: int
    reason: str


@app.get("/api/tickets")
def api_tickets_list(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, target: str = "duckdb",
                     status: str | None = None):
    _check_domain(request, domain)
    return {"domain": domain, "tickets": ops_tickets.list_tickets(domain, target, status)}


@app.post("/api/tickets/scan")
def api_tickets_scan(request: Request, domain: str = Form(...), target: str = Form("duckdb")):
    """Runs the real per-layer test pack and raises one tracked ticket per currently-failing
    case that isn't already tracked -- see emitters/ops_tickets.py."""
    _check_domain(request, domain)
    return ops_tickets.scan(domain, target)


@app.post("/api/tickets/assign")
def api_tickets_assign(request: Request, body: TicketAssignRequest):
    _check_domain(request, body.domain)
    return ops_tickets.assign(body.domain, body.target, body.ticket_id, body.assigned_to)


@app.post("/api/tickets/propose-fix")
def api_tickets_propose_fix(request: Request, body: TicketFixRequest):
    _check_domain(request, body.domain)
    return ops_tickets.propose_fix(body.domain, body.target, body.ticket_id, body.resolution_note)


@app.post("/api/tickets/reject")
def api_tickets_reject(request: Request, body: TicketRejectRequest):
    _check_domain(request, body.domain)
    return ops_tickets.reject(body.domain, body.target, body.ticket_id, body.reason)


@app.get("/api/journey")
def api_journey(request: Request, domain: str = pipeline.DEFAULT_DOMAIN, target: str = "duckdb"):
    """The five-step journey plus where this domain actually stands in it. Renders from
    agents/gates.py, the same registry that decides what interrupts the agent graph."""
    _check_domain(request, domain)
    try:
        return journey.journey(domain, target, allowed_domains=_user_domains(request))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/sdlc")
def api_sdlc():
    return sdlc.stage_status()


@app.get("/api/uploads")
def api_uploads_list(request: Request):
    # A company login only ever sees its own domain's uploads -- admin sees everything, the
    # same "*" vs explicit-list pattern every other domain-scoped route already uses.
    allowed = _user_domains(request)
    records = uploads.list_uploads()
    if allowed != "*":
        records = [r for r in records if uploads.workbook_domain(r) in allowed]
    return {"uploads": records}


@app.post("/api/uploads")
async def api_uploads_create(request: Request, kind: str = Form(...), file: UploadFile = File(...)):
    # Preview is a dry run -- compile_workbook never writes to contracts/ -- so it's safe to let
    # any logged-in account (scoped or admin) try one. Real enforcement happens at approve time
    # below, where a write is actually about to happen.
    user = getattr(request.state, "user", None)
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="only .xlsx workbooks are accepted")
    content = await file.read()
    try:
        return uploads.save_and_preview(kind, file.filename, content,
                                        uploaded_by=user["username"] if user else "local")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/uploads/{upload_id}/approve")
def api_uploads_approve(request: Request, upload_id: str):
    # A scoped company login may only freeze a workbook that declares ITS OWN domain -- adding
    # or amending their own sources is a real, legitimate self-service action; creating an
    # entirely new domain from scratch (a workbook declaring a domain the account has no
    # existing access to) stays admin-only, since there's no existing scope to check it against.
    # Re-derived fresh from the stored workbook, not trusted from the earlier preview call.
    records = {r["id"]: r for r in uploads.list_uploads()}
    record = records.get(upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no upload {upload_id!r}")
    domain = uploads.workbook_domain(record)
    if domain is None:
        raise HTTPException(status_code=400, detail="could not determine this workbook's domain")
    _check_domain(request, domain)
    try:
        return uploads.approve_and_compile(upload_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/agent-runs")
def api_agent_runs_list(request: Request):
    allowed = _user_domains(request)
    runs = agent_runs.list_runs()
    if allowed != "*":
        runs = [r for r in runs if r.get("domain") in allowed]
    return {"runs": runs}


@app.get("/api/agent-runs/{run_id}")
def api_agent_runs_detail(request: Request, run_id: str):
    try:
        detail = agent_runs.get_run_detail(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _check_domain(request, detail.get("domain", ""))
    return detail


@app.post("/api/agent-runs")
def api_agent_runs_start(request: Request, upload_id: str = Form(...), domain_hint: str = Form("unknown")):
    """Starts a real agent run against an already-uploaded workbook (see /api/uploads) -- the
    agent previews it, and if it looks reasonable, attempts to Freeze it, which pauses at the
    real gate proven in evidence/runs/phase-c-agent-activation.md for a human to approve here."""
    _require_admin(request)
    record = next((u for u in uploads.list_uploads() if u["id"] == upload_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no upload {upload_id!r}")
    return agent_runs.start_run(record["stored_path"], domain_hint)


@app.post("/api/agent-runs/validate")
def api_agent_runs_start_validation(request: Request, domain: str = Form(...), target: str = Form("duckdb")):
    """Starts a real agent run that drives Step 04 validation for a domain that already has
    approved contracts and real data -- no workbook needed. The PM agent runs the per-layer
    test pack itself, gathers the evidence pack, and attempts G3 sign-off, which pauses for a
    human decision here exactly like the intake Freeze gate does."""
    _check_domain(request, domain)
    return agent_runs.start_validation_run(domain, target)


@app.post("/api/agent-runs/{run_id}/approve")
def api_agent_runs_approve(request: Request, run_id: str):
    try:
        detail = agent_runs.get_run_detail(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _check_domain(request, detail.get("domain", ""))
    try:
        return agent_runs.resume_run(run_id, "approve")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/agent-runs/{run_id}/reject")
def api_agent_runs_reject(request: Request, run_id: str, message: str = Form("")):
    try:
        detail = agent_runs.get_run_detail(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _check_domain(request, detail.get("domain", ""))
    try:
        return agent_runs.resume_run(run_id, "reject", message or None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
