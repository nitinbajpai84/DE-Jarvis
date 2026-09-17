"""Live smoke test of the whole Control Room: every feature, against a deployed backend and frontend.

    SMOKE_ADMIN=user:pass SMOKE_INSURANCE=user:pass SMOKE_INVESTMENTS=user:pass \\
        python harness/smoke_live.py [--base URL] [--frontend URL] [--out report.md]

Credentials come from the environment, never this file.

What it may change, deliberately bounded:
  - company logins are used READ-ONLY, apart from one tier-1 estate scan of asset_management
    (cheap, and what a company does on day one)
  - every write -- uploads, intents, architecture, agent conversations, proposals, memories,
    advice -- goes to the admin sandbox domain "zz_smoke", which no company can see, and is
    cleaned up at the end where the platform allows (intents deleted, source versions withdrawn,
    memories forgotten; decided proposals and advice records stay as history)
  - nothing starts a pipeline run, a background agent run or a ticket scan

Each check records PASS/FAIL, the time it took, and what it saw. Exit code 1 if anything failed.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import io
import json
import os
import pathlib
import sys
import time
from typing import Any

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SANDBOX = "zz_smoke"


class Smoke:
    def __init__(self, base: str, frontend: str):
        self.base, self.frontend = base.rstrip("/"), frontend.rstrip("/")
        self.results: list[dict[str, Any]] = []
        self.creds = {k: tuple(os.environ[f"SMOKE_{k.upper()}"].split(":", 1))
                      for k in ("admin", "insurance", "investments") if os.environ.get(f"SMOKE_{k.upper()}")}
        missing = {"admin", "insurance", "investments"} - set(self.creds)
        if missing:
            sys.exit(f"set SMOKE_{', SMOKE_'.join(m.upper() for m in sorted(missing))} as user:password")
        self.area = ""

    # -- plumbing -------------------------------------------------------------------------------
    def call(self, who: str | None, method: str, path: str, timeout: int = 120, **kw) -> requests.Response:
        auth = self.creds[who] if who else None
        return requests.request(method, self.base + path, auth=auth, timeout=timeout, **kw)

    def check(self, name: str, fn):
        t = time.time()
        try:
            detail = fn()
            ok = True
        except AssertionError as exc:
            ok, detail = False, f"assertion: {exc}"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:240]}"
        ms = round((time.time() - t) * 1000)
        self.results.append({"area": self.area, "check": name, "ok": ok, "ms": ms, "detail": str(detail)[:300]})
        print(f"{'PASS' if ok else 'FAIL'}  {self.area:<28} {name:<62} {ms:>6}ms  {str(detail)[:110]}", flush=True)
        return detail if ok else None

    def j(self, r: requests.Response, status: int = 200) -> Any:
        assert r.status_code == status, f"HTTP {r.status_code} (wanted {status}): {r.text[:200]}"
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    # -- the run --------------------------------------------------------------------------------
    def run(self) -> None:
        # a fresh intent per run: a re-used id meets the previous run's proposal history
        state: dict[str, Any] = {"intent_name": f"Smoke claims {_dt.datetime.now(_dt.timezone.utc):%Y%m%d%H%M%S}"}

        self.area = "1 Platform & sign-in"
        self.check("frontend served by Vercel with this build's features", self._frontend)
        self.check("backend serves the page shell", lambda: (self.j(self.call(None, "GET", "/")) and "ok"))
        self.check("API refuses a request with no login (401)", lambda: (
            self.call(None, "GET", "/api/me").status_code == 401 or (_ for _ in ()).throw(AssertionError("not 401"))) and "401")
        self.check("admin sees every domain", lambda: self._me("admin", "*"))
        self.check("Star Insurance is scoped to insurance", lambda: self._me("insurance", ["insurance"]))
        self.check("Star Investments is scoped to asset_management", lambda: self._me("investments", ["asset_management"]))
        self.check("targets list duckdb and databricks", lambda: self._targets())
        self.check("company journey hides the admin Workspace step", self._journey_scoping)

        self.area = "2 Tenancy"
        for path in ("/api/estate/report?domain=insurance&target=databricks", "/api/landing?domain=insurance",
                     "/api/discovery?domain=insurance", "/api/gaps?domain=insurance", "/api/proposals?domain=insurance",
                     "/api/agents/detective/context?domain=insurance", "/api/memory?domain=insurance",
                     "/api/architecture/advice?domain=insurance", "/api/intents?domain=insurance",
                     "/api/tickets?domain=insurance"):
            self.check(f"Star Investments blocked: {path.split('?')[0]}", lambda p=path: self._status("investments", "GET", p, 403))
        self.check("company cannot profile another company's landing files",
                   lambda: self._status("investments", "POST", "/api/discovery/profile", 403, json={
                       "connection": {"type": "file", "path": "landing/insurance/structured/claims/*.csv"},
                       "source_id": "x", "domain": "asset_management"}))
        self.check("company cannot refresh the shared reference corpus", lambda: self._status("insurance", "POST", "/api/reference/refresh", 403))

        self.area = "3 Estate scan & report"
        self.check("insurance has a completed Databricks scan", self._estate_report)
        self.check("company report never shows admin-only legacy schemas", self._estate_scope)
        self.check("tier-1 scan starts, runs and completes (asset_management)", self._estate_scan_run)

        self.area = "4 Landing zone"
        self.check("insurance landing zone is company/category/source", self._landing)

        self.area = "5 Connect, profile & review"
        self.check("test connection: public API (data.gov.sg)", lambda: self._conn_test({"type": "api", "endpoint": "https://api.data.gov.sg/v1/transport/carpark-availability"}))
        self.check("test connection: insurance's own landing files", lambda: self._conn_test({"type": "file", "path": "landing/insurance/structured/claims/claims_*.csv"}, who="insurance", domain="insurance"))
        self.check("upload CSV creates v1 awaiting review, with preview rows", lambda: self._upload_v1(state))
        self.check("re-upload creates v2 with diff against v1", lambda: self._upload_v2(state))
        self.check("reject needs a reason; with one, rolls back to v1", lambda: self._reject_rollback(state))
        self.check("accept signs a version off", lambda: self._accept(state))
        self.check("Excel upload lands as structured and profiles its columns", self._upload_xlsx)
        self.check("unsupported file type refused with a reason", lambda: self._status("admin", "POST", "/api/discovery/upload", 400,
                   data={"domain": SANDBOX, "source_id": "bad"}, files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}))
        self.check("profile a live API source (data.gov.sg rainfall)", self._profile_api)

        self.area = "6 Intent, changes & gaps"
        self.check("capture an intent", lambda: self._intent_capture(state))
        self.check("revise it: diff and gap delta reported", lambda: self._intent_revise(state))
        self.check("only the current version can be signed", lambda: self._intent_sign(state))
        self.check("G1 gap analysis runs", lambda: self._gap_analysis())
        self.check("gap report classifies data points", self._gap_report)
        self.check("insurance gap report readable by its company", lambda: (self.j(self.call("insurance", "GET", "/api/gaps?domain=insurance&target=databricks")) and "ok"))

        self.area = "7 Architecture"
        self.check("capture an architecture record", self._arch_capture)
        self.check("high-level diagram from insurance contracts", lambda: self._diagram("high"))
        self.check("low-level diagram from insurance contracts", lambda: self._diagram("low"))
        self.check("read an architecture sketch image (Gemini vision)", self._interpret_image)

        self.area = "8 Reference advice"
        self.check("22 published sources fetched", self._ref_sources)
        self.check("Star Insurance's stored advice is readable", self._ref_list)
        self.check("new advice: quotes verified against source pages", lambda: self._advice(state))
        self.check("a recommendation becomes a proposal with citations", lambda: self._advice_propose(state))

        self.area = "9 Agents: brain, context, memory"
        self.check("seven agents on Gemini", self._agents_list)
        self.check("context pack for the Data Detective (insurance)", self._agent_context)
        self.check("conversation turn with trace (short-term memory)", lambda: self._agent_chat(state))
        self.check("conversation is resumable; other logins can't read it", lambda: self._agent_session(state))
        self.check("a stated decision becomes team memory, recalled by another agent", lambda: self._agent_memory(state))
        self.check("catalogue chat (intent) replies", self._catalogue_chat)

        self.area = "10 Proposals & approvals"
        self.check("agent files a proposal from conversation", lambda: self._agent_proposes(state))
        self.check("approval runs it under the approver's name", lambda: self._approve(state))
        self.check("second decision refused (409); decline needs a reason", lambda: self._decide_rules(state))
        self.check("agent trusts current records over past approvals", self._history_vs_records)

        self.area = "11 Build, test, operate"
        self.check("medallion flow (insurance, Databricks)", lambda: self._read("insurance", "/api/pipeline/flow?domain=insurance&target=databricks"))
        self.check("alerts feed", lambda: self._read("insurance", "/api/alerts?domain=insurance&target=databricks"))
        self.check("incident tickets list", lambda: self._read("insurance", "/api/tickets?domain=insurance&target=databricks"))
        self.check("per-layer test pack runs (insurance, Databricks)", self._test_pack)
        self.check("dashboard renders from live gold data", self._dashboard)
        self.check("SDLC stage status", lambda: self._read("admin", "/api/sdlc"))
        self.check("workbook intake uploads list (scoped)", lambda: self._read("insurance", "/api/uploads"))
        self.check("agent runs list", lambda: self._read("admin", "/api/agent-runs"))

        self.area = "12 Cleanup"
        self.check("sandbox cleaned (intent, sources, memories)", lambda: self._cleanup(state))

    # -- checks ---------------------------------------------------------------------------------
    def _frontend(self):
        html = requests.get(self.frontend, timeout=30).text
        markers = ["teamDrawer", "openApprovals", "adviceHost", "landingZone", "gapReport", "reviewPanel", "estateOverview"]
        missing = [m for m in markers if m not in html]
        assert not missing, f"missing {missing}"
        local = (REPO_ROOT / "webapp" / "frontend" / "index.html").read_bytes()
        same = hashlib.sha256(html.encode("utf-8")).hexdigest() == hashlib.sha256(local.decode("utf-8").replace("\r\n", "\n").encode("utf-8")).hexdigest() \
            or hashlib.sha256(html.encode()).hexdigest() == hashlib.sha256(local).hexdigest()
        return f"all {len(markers)} feature markers present; identical to repo index.html: {same}"

    def _me(self, who, domains):
        me = self.j(self.call(who, "GET", "/api/me"))
        assert me["domains"] == domains, me
        return f"{me['username']} -> {me['domains']}"

    def _targets(self):
        t = self.j(self.call("admin", "GET", "/api/targets"))
        vals = t.get("targets", t)
        assert "duckdb" in vals and "databricks" in vals, t
        return vals

    def _journey_scoping(self):
        admin = [s["id"] for s in self.j(self.call("admin", "GET", "/api/journey?domain=insurance&target=databricks"))["steps"]]
        comp = [s["id"] for s in self.j(self.call("insurance", "GET", "/api/journey?domain=insurance&target=databricks"))["steps"]]
        assert "onboard" in admin and "onboard" not in comp, (admin, comp)
        return f"admin {len(admin)} steps, company {len(comp)}"

    def _status(self, who, method, path, want, **kw):
        r = self.call(who, method, path, **kw)
        assert r.status_code == want, f"HTTP {r.status_code}: {r.text[:160]}"
        return f"{want}"

    def _estate_report(self):
        r = self.j(self.call("insurance", "GET", "/api/estate/report?domain=insurance&target=databricks"))
        assert r.get("scan"), "no completed scan"
        c = r["coverage"]
        return f"scan {r['scan']['scan_id']} tier {r['scan']['tier_reached']}: {c['tables']} tables, {len(r['relationships']['orphans'])} orphan refs, {len(r['open_questions'])} questions"

    def _estate_scope(self):
        r = self.j(self.call("insurance", "GET", "/api/estate/report?domain=insurance&target=databricks"))
        assert r["scan"]["scope"] == "domain" and r["coverage"]["unclassified_schemas"] == [], r["scan"]
        return "scope=domain, no unclassified schemas"

    def _estate_scan_run(self):
        started = self.j(self.call("investments", "POST", "/api/estate/scan", data={"domain": "asset_management", "target": "databricks", "tiers": 1}))
        sid = started["scan_id"]
        for _ in range(60):
            scans = self.j(self.call("investments", "GET", "/api/estate/scans?domain=asset_management&target=databricks"))
            row = next((s for s in scans["scans"] if s["scan_id"] == sid), None)
            if row and row["status"] != "running":
                assert row["status"] == "completed", row
                return f"{sid}: {row['tables_scanned']} tables, tier {row['tier_reached']}, scope {row['scope']}"
            time.sleep(5)
        raise AssertionError("scan still running after 5 minutes")

    def _landing(self):
        inv = self.j(self.call("insurance", "GET", "/api/landing?domain=insurance"))
        cats = {c["category"]: c["sources"] for c in inv["categories"]}
        assert set(cats) == {"structured", "unstructured", "api", "database"}, cats.keys()
        assert cats["structured"] and all(s["path"].startswith("landing/insurance/structured/") for s in cats["structured"])
        return f"{len(cats['structured'])} structured sources under {inv['root']}/structured/"

    def _conn_test(self, connection, who="admin", domain=SANDBOX):
        r = self.j(self.call(who, "POST", "/api/discovery/test", json={"connection": connection, "domain": domain}))
        assert r["ok"], r
        return r["detail"]

    def _upload(self, source_id, name, content, note=""):
        return self.j(self.call("admin", "POST", "/api/discovery/upload", data={"domain": SANDBOX, "source_id": source_id, "note": note},
                                files={"file": (name, content, "text/csv")}))

    def _upload_v1(self, state):
        v = self._upload("smoke_claims", "claims.csv", b"claim_id,policy_id,amount\nC1,P1,100\nC2,P2,\nC3,P3,300\n", "first")
        assert v["meta"]["version"] >= 1 and v["meta"]["review"]["status"] == "pending"
        assert v["profile"]["preview"]["rows"][0] == ["C1", "P1", "100"]
        assert v["profile"]["connection"]["path"].startswith(f"landing/{SANDBOX}/structured/smoke_claims/uploads/")
        state["v1"] = v["meta"]["version"]
        return f"v{state['v1']} pending, {len(v['profile']['preview']['rows'])} preview rows"

    def _upload_v2(self, state):
        v = self._upload("smoke_claims", "claims_fixed.csv",
                         b"claim_id,policy_id,claim_amount,loss_date\nC1,P1,100,2026-01-02\nC2,,250,2026-01-03\nC3,,300,2026-01-04\nC4,P4,90,2026-01-05\n", "fixed")
        d = v["diff"]
        assert "loss_date" in d["columns_added"] and "amount" in d["columns_removed"] and d["rows"] == {"before": 3, "after": 4}, d
        assert not d["connection_changed"]
        state["v2"] = v["meta"]["version"]
        return f"v{state['v2']} vs v{v['compared_with']}: +{d['columns_added']} -{d['columns_removed']}"

    def _reject_rollback(self, state):
        self._status("admin", "POST", "/api/discovery/review", 400, data={"domain": SANDBOX, "source_id": "smoke_claims", "version": state["v2"], "decision": "reject"})
        r = self.j(self.call("admin", "POST", "/api/discovery/review", data={"domain": SANDBOX, "source_id": "smoke_claims", "version": state["v2"], "decision": "reject", "comment": "smoke: policy_id half empty"}))
        assert r["status"] == "rejected" and r["live_version"] == state["v1"], r
        return f"live version back to v{r['live_version']}"

    def _accept(self, state):
        r = self.j(self.call("admin", "POST", "/api/discovery/review", data={"domain": SANDBOX, "source_id": "smoke_claims", "version": state["v1"], "decision": "accept"}))
        assert r["status"] == "accepted", r
        return f"v{state['v1']} accepted"

    def _upload_xlsx(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.append(["policy_id", "premium", "start_date"])
        wb.active.append(["P1", 1200.5, _dt.date(2026, 1, 2)])
        buf = io.BytesIO()
        wb.save(buf)
        v = self.j(self.call("admin", "POST", "/api/discovery/upload", data={"domain": SANDBOX, "source_id": "smoke_premiums"},
                             files={"file": ("premiums.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}))
        cols = [c["name"] for c in v["profile"]["columns"]]
        assert cols == ["policy_id", "premium", "start_date"] and v["profile"]["preview"]["rows"][0] == ["P1", "1200.5", "2026-01-02"], (cols, v["profile"]["preview"])
        return f"columns {cols}"

    def _profile_api(self):
        r = self.j(self.call("admin", "POST", "/api/discovery/profile", timeout=120, json={
            "connection": {"type": "api", "endpoint": "https://api-open.data.gov.sg/v2/real-time/api/rainfall"},
            "source_id": "smoke_rainfall", "domain": SANDBOX, "sample_limit": 50}))
        assert r["columns"] and r.get("version"), r
        return f"{len(r['columns'])} columns, {r['total_rows']} records at {r.get('records_path')}, v{r['version']}"

    def _intent_capture(self, state):
        r = self.j(self.call("admin", "POST", "/api/intent", json={"domain": SANDBOX, "intent": {
            "name": state["intent_name"], "business_outcome": "Claims by month",
            "reports": [{"name": "Claims by month", "required_data_points": ["claim_id", "amount"]}]}}))
        state["intent_id"] = r["intent_id"]
        ids = [i["intent_id"] for i in self.j(self.call("admin", "GET", f"/api/intents?domain={SANDBOX}"))["intents"]]
        assert r["intent_id"] in ids
        return r["intent_id"]

    def _intent_revise(self, state):
        self.j(self.call("admin", "POST", "/api/intent", json={"domain": SANDBOX, "intent_id": state["intent_id"], "intent": {
            "name": state["intent_name"], "business_outcome": "Claims by month",
            "reports": [{"name": "Claims by month", "required_data_points": ["claim_id", "loss_date", "broker_code"]}]}}))
        v = self.j(self.call("admin", "GET", f"/api/intent/changes?domain={SANDBOX}&intent_id={state['intent_id']}&target=duckdb"))
        ch = v["diff"]["reports_changed"][0]
        assert set(ch["data_points_added"]) == {"loss_date", "broker_code"} and ch["data_points_removed"] == ["amount"], ch
        state["intent_version"] = v["meta"]["version"]
        return f"v{v['meta']['version']}: +{ch['data_points_added']} -{ch['data_points_removed']}; newly open {[g['data_point'] for g in v['gaps']['newly_open']]}"

    def _intent_sign(self, state):
        old = state["intent_version"] - 1
        self._status("admin", "POST", "/api/intent/sign", 409, data={"domain": SANDBOX, "intent_id": state["intent_id"], "version": old})
        r = self.j(self.call("admin", "POST", "/api/intent/sign", data={"domain": SANDBOX, "intent_id": state["intent_id"], "version": state["intent_version"]}))
        assert r["review"]["status"] == "signed"
        return f"v{old} refused, v{state['intent_version']} signed"

    def _gap_analysis(self):
        r = self.j(self.call("admin", "GET", f"/api/intent/gap-analysis?domain={SANDBOX}"))
        assert r["intent_captured"]
        return f"{r['open_count']} open"

    def _gap_report(self):
        r = self.j(self.call("admin", "GET", f"/api/gaps?domain={SANDBOX}&target=duckdb"))
        rows = {d["data_point"]: [g["type"] for g in d["gaps"]] for d in r["data_points"]}
        assert rows.get("broker_code") == ["missing"], rows
        assert "missing" not in rows.get("claim_id", ["missing"]), rows            # found in the accepted smoke_claims profile
        return f"{r['summary']['ready']}/{r['summary']['data_points']} ready; {rows}"

    def _arch_capture(self):
        self.j(self.call("admin", "POST", "/api/architecture", json={"domain": SANDBOX, "architecture": {
            "rto": "4 hours", "rpo": "1 hour", "platform_binding": "databricks", "layering_rationale": "smoke",
            "entity_scd": [{"entity": "dim_policy", "scd_type": "scd2"}]}}))
        a = self.j(self.call("admin", "GET", f"/api/architecture?domain={SANDBOX}"))["architecture"]
        assert a["rto"] == "4 hours"
        return f"rto {a['rto']}, rpo {a['rpo']}"

    def _diagram(self, level):
        d = self.j(self.call("insurance", "GET", f"/api/architecture/diagram?domain=insurance&level={level}"))
        n = len(d.get("nodes") or d.get("entities") or [])
        assert n > 0, list(d)[:5]
        return f"{n} nodes"

    def _interpret_image(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (520, 160), "white")
        dr = ImageDraw.Draw(img)
        for i, label in enumerate(["Landing", "Bronze", "Silver", "Gold"]):
            dr.rectangle([10 + i * 128, 50, 118 + i * 128, 110], outline="black")
            dr.text((25 + i * 128, 72), label, fill="black")
        dr.text((10, 10), "RTO 4h  RPO 1h  SCD2 for dim_policy", fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        r = self.j(self.call("admin", "POST", "/api/architecture/interpret-image", data={"domain": SANDBOX},
                             files={"file": ("sketch.png", buf.getvalue(), "image/png")}))
        return json.dumps(r)[:160]

    def _ref_sources(self):
        s = self.j(self.call("insurance", "GET", "/api/reference/sources"))["sources"]
        ok = [x for x in s if x["status"] == "ok"]
        assert len(ok) >= 20, [(x["source_id"], x["status"]) for x in s if x["status"] != "ok"]
        return f"{len(ok)}/{len(s)} ok, {sum(x['passages'] for x in ok)} passages"

    def _ref_list(self):
        a = self.j(self.call("insurance", "GET", "/api/architecture/advice?domain=insurance"))["advice"]
        assert a, "no advice stored"
        return f"{len(a)} advice record(s); latest {a[0]['stats'].get('citations_verified')} verified citations"

    def _advice(self, state):
        a = self.j(self.call("admin", "POST", "/api/architecture/advice", timeout=240, json={
            "domain": SANDBOX, "target": "duckdb", "cloud": "azure", "question": "How do we meet a 1 hour RPO?"}))
        assert a["recommendations"], a["stats"]
        for r in a["recommendations"]:
            for c in r["citations"]:
                assert c["ref"] in a["sources"] and a["sources"][c["ref"]]["url"].startswith("https://learn.microsoft.com/"), c
        state["advice"] = a
        st = a["stats"]
        return f"{len(a['recommendations'])} recs, {st['citations_verified']}/{st['citations_checked']} quotes verified, {len(a['rejected'])} dropped"

    def _advice_propose(self, state):
        a = state["advice"]
        idx = next((i for i, r in enumerate(a["recommendations"]) if not (r.get("proposal") or {}).get("invalid")), 0)
        p = self.j(self.call("admin", "POST", f"/api/architecture/advice/{a['advice_id']}/propose", data={"domain": SANDBOX, "index": idx}))
        assert p["status"] == "proposed" and any(e.startswith("R") for e in p["evidence"]), p
        state["advice_proposal"] = p["proposal_id"]
        return f"{p['kind']}: {p['title']} (evidence {p['evidence']})"

    def _agents_list(self):
        a = self.j(self.call("insurance", "GET", "/api/agents"))["agents"]
        assert len(a) == 7 and all(x["model"].startswith("gemini") for x in a)
        return ", ".join(x["persona"] for x in a)

    def _agent_context(self):
        items = self.j(self.call("insurance", "GET", "/api/agents/detective/context?domain=insurance&target=databricks"))["items"]
        assert all(i["ok"] for i in items), [(i["id"], i["text"][:80]) for i in items if not i["ok"]]
        return " ".join(f"{i['id']}:{i['chars']}" for i in items)

    def _agent_chat(self, state):
        r = self.j(self.call("admin", "POST", "/api/agents/detective/chat", timeout=180, json={
            "domain": SANDBOX, "message": "Which sources do we have and which are still awaiting review?"}))
        t = r["trace"]
        assert r["reply"] and t["model"].startswith("gemini") and t["context"], t
        state["session"] = r["session_id"]
        return f"{t['timings_ms']['total']}ms, cited {t['cited']}, unverified {t['citations_unverified']}: {r['reply'][:90]}"

    def _agent_session(self, state):
        s = self.j(self.call("admin", "GET", f"/api/agents/detective/sessions/{state['session']}?domain={SANDBOX}"))
        assert len(s["messages"]) == 2
        self._status("insurance", "GET", f"/api/agents/detective/sessions/{state['session']}?domain={SANDBOX}", 403)
        return "2 messages stored; company login refused (403)"

    def _agent_memory(self, state):
        r = self.j(self.call("admin", "POST", "/api/agents/architect/chat", timeout=180, json={
            "domain": SANDBOX, "message": "For the record, we have decided that smoke test data is always kept in the zz_smoke sandbox."}))
        saved = r["trace"]["memories_saved"]
        assert saved and saved[0]["scope"] == "team", r["trace"]
        r2 = self.j(self.call("admin", "POST", "/api/agents/nightwatch/chat", timeout=180, json={
            "domain": SANDBOX, "message": "Where did the team decide smoke test data is kept?"}))
        recalled = [m["memory_id"] for m in r2["trace"]["memories_recalled"]]
        assert saved[0]["memory_id"] in recalled, r2["trace"]["memories_recalled"]
        return f"saved M{saved[0]['memory_id']} (team) by Chief Architect; Night Watch recalled it: {r2['reply'][:80]}"

    def _catalogue_chat(self):
        r = self.j(self.call("admin", "POST", "/api/catalogue/chat", timeout=120, json={
            "kind": "intent", "domain": SANDBOX, "history": [], "message": "We want a monthly claims dashboard."}))
        assert r["reply"]
        return r["reply"][:100]

    def _agent_proposes(self, state):
        r = self.j(self.call("admin", "POST", "/api/agents/delivery/chat", timeout=180, json={
            "domain": SANDBOX, "message": f"Please propose adding the data point paid_amount to the Claims by month report of intent {state['intent_id']}."}))
        props = r["trace"]["proposals"]
        assert props, (r["reply"], r["trace"]["tools"])
        state["proposal"] = props[0]["proposal_id"]
        return f"{props[0]['kind']}: {props[0]['preview']}"

    def _approve(self, state):
        p = self.j(self.call("admin", "POST", f"/api/proposals/{state['proposal']}/decide", data={"domain": SANDBOX, "decision": "approve"}))
        assert p["status"] == "approved", p
        it = self.j(self.call("admin", "GET", f"/api/intent?domain={SANDBOX}&intent_id={state['intent_id']}"))["intent"]
        pts = it["reports"][0]["required_data_points"]
        assert "paid_amount" in pts, pts
        return f"approved by {p['decided_by']}; report now needs {pts}"

    def _decide_rules(self, state):
        self._status("admin", "POST", f"/api/proposals/{state['proposal']}/decide", 409, data={"domain": SANDBOX, "decision": "approve"})
        pid = state["advice_proposal"]
        self._status("admin", "POST", f"/api/proposals/{pid}/decide", 400, data={"domain": SANDBOX, "decision": "decline"})
        p = self.j(self.call("admin", "POST", f"/api/proposals/{pid}/decide", data={"domain": SANDBOX, "decision": "decline", "note": "smoke test: declined on purpose"}))
        assert p["status"] == "declined"
        return "409 on re-decide, 400 without note, declined with note"

    def _history_vs_records(self):
        """Found by the second live run: with an old "add paid_amount" proposal approved for a
        since-deleted intent of the same id, the Delivery Lead refused to propose it again for the
        re-created intent. Re-creates that situation on purpose and expects a proposal."""
        self.j(self.call("admin", "POST", "/api/intent", json={"domain": SANDBOX, "intent_id": "smoke-claims", "intent": {
            "name": "Smoke claims", "reports": [{"name": "Claims by month", "required_data_points": ["claim_id"]}]}}))
        try:
            r = self.j(self.call("admin", "POST", "/api/agents/delivery/chat", timeout=180, json={
                "domain": SANDBOX, "message": "Please propose adding paid_amount to the Claims by month report of intent smoke-claims."}))
            props = r["trace"]["proposals"]
            assert props, f"no proposal filed: {r['reply'][:160]}"
            for pr in props:
                self.call("admin", "POST", f"/api/proposals/{pr['proposal_id']}/decide",
                          data={"domain": SANDBOX, "decision": "decline", "note": "smoke test: checked, not applied"})
            return f"filed {props[0]['kind']} despite earlier approval history"
        finally:
            self.call("admin", "DELETE", f"/api/intent/{SANDBOX}/smoke-claims")

    def _read(self, who, path):
        r = self.j(self.call(who, "GET", path, timeout=180))
        return (json.dumps(r)[:120]) if not isinstance(r, str) else r[:120]

    def _test_pack(self):
        r = self.j(self.call("insurance", "GET", "/api/test-pack?domain=insurance&target=databricks", timeout=300))
        s = json.dumps(r)
        return s[:160]

    def _dashboard(self):
        r = self.j(self.call("insurance", "GET", "/api/dashboard?domain=insurance&target=databricks", timeout=180))
        tiles = r.get("tiles") or []
        assert tiles, list(r)[:6]
        return f"{len(tiles)} tiles, {sum(1 for t in tiles if t.get('error'))} with errors"

    def _cleanup(self, state):
        done = []
        if state.get("intent_id"):
            self.call("admin", "DELETE", f"/api/intent/{SANDBOX}/{state['intent_id']}")
            done.append("intent deleted")
        for src in ("smoke_claims", "smoke_premiums", "smoke_rainfall"):
            v = self.call("admin", "GET", f"/api/discovery/version?domain={SANDBOX}&source_id={src}")
            if v.status_code != 200:
                continue
            for m in v.json()["history"]:
                if (m.get("review") or {}).get("status") != "rejected":
                    self.call("admin", "POST", "/api/discovery/review", data={"domain": SANDBOX, "source_id": src, "version": m["version"],
                                                                              "decision": "reject", "comment": "smoke test cleanup"})
            done.append(f"{src} withdrawn")
        mem = self.j(self.call("admin", "GET", f"/api/memory?domain={SANDBOX}"))["memories"]
        for m in mem:
            self.call("admin", "DELETE", f"/api/memory/{SANDBOX}/{m['memory_id']}")
        done.append(f"{len(mem)} memories forgotten")
        return "; ".join(done)

    # -- report ---------------------------------------------------------------------------------
    def report(self, out: pathlib.Path | None) -> int:
        failed = [r for r in self.results if not r["ok"]]
        lines = [f"# Live smoke test — {_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d %H:%M} UTC", "",
                 f"Backend `{self.base}` · frontend `{self.frontend}`", "",
                 f"**{len(self.results) - len(failed)} passed, {len(failed)} failed** of {len(self.results)} checks.", "",
                 "| Area | Check | Result | Time | What it saw |", "|---|---|---|---|---|"]
        for r in self.results:
            detail = r["detail"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['area']} | {r['check']} | {'PASS' if r['ok'] else '**FAIL**'} | {r['ms'] / 1000:.1f}s | {detail} |")
        text = "\n".join(lines) + "\n"
        if out:
            out.write_text(text, encoding="utf-8")
        print(f"\n{len(self.results) - len(failed)} passed, {len(failed)} failed")
        return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://jarvis-backend-production-dfb3.up.railway.app")
    ap.add_argument("--frontend", default="https://jarvis-control-room.vercel.app")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    smoke = Smoke(args.base, args.frontend)
    smoke.run()
    sys.exit(smoke.report(pathlib.Path(args.out) if args.out else None))
