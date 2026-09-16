"""Generates a static HTML snapshot of the bronze-layer control plane state --
harness/dashboard.html -- for reviewing what's been ingested, what passed/failed, and why,
before signing off a phase. Run after any bronze_loader run to refresh it; pass --target
databricks to report on that platform's control plane instead of the local duckdb one (each
target's control/bronze schemas are independent, per contracts/platform/<target>.yaml, so a
single dashboard run reports on exactly one target at a time).
"""
from __future__ import annotations

import argparse
import html
import pathlib
import sys
from datetime import datetime, timezone

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "harness" / "dashboard.html"
DOMAIN = "insurance"  # this snapshot is scoped to one domain -- see the multi-domain design


def _contract(source_id: str) -> dict:
    matches = list((REPO_ROOT / "contracts" / "sources").glob(f"*/{source_id}.source.yaml"))
    return yaml.safe_load(matches[0].read_text()) if matches else {}


def _load_platform(target: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def gather(target: str = "duckdb") -> dict:
    platform = _load_platform(target)
    control = resolve_schema(platform, DOMAIN, "control")
    bronze = resolve_schema(platform, DOMAIN, "bronze")
    con = sql_connect(target, platform)
    sources = [r[0] for r in con.execute(
        f"select distinct source_id from {control}.run_registry where domain = ? order by source_id",
        [DOMAIN],
    ).fetchall()]

    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "target": target, "sources": []}

    for sid in sources:
        contract = _contract(sid)
        ctype = contract.get("connection", {}).get("type", "unknown")
        domain = contract.get("domain", "")

        runs = con.execute(
            f"select run_id, status, error_message, started_at, files_seen, files_accepted, "
            f"files_quarantined, rows_loaded from {control}.run_registry where source_id = ? "
            f"order by started_at", [sid],
        ).fetchall()
        latest = runs[-1] if runs else None

        try:
            bronze_rows = con.execute(f"select count(*) from {bronze}.{sid}").fetchone()[0]
        except Exception:  # noqa: BLE001 -- exception type differs by driver; absence just means 0 rows
            bronze_rows = 0

        batches = con.execute(
            f"select fa.file_name, fa.size_kb, fa.row_count, fa.column_count, fa.fqc_passed, "
            f"fa.action, fa.arrival_time, doc.data_catalogue_id "
            f"from {control}.file_audit fa "
            f"left join {control}.data_object_catalogue doc "
            f"  on doc.file_name = fa.file_name and doc.data_source_id = ? "
            f"where fa.run_id in (select run_id from {control}.run_registry where source_id = ?) "
            f"order by fa.arrival_time", [sid, sid],
        ).fetchall()

        dq = con.execute(
            f"select dq.rule_type, dq.columns, dq.severity, dq.passed, dq.failed_row_count, "
            f"doc.file_name "
            f"from {control}.dq_results dq "
            f"join {control}.data_object_catalogue doc on dq.data_catalogue_id = doc.data_catalogue_id "
            f"where doc.data_source_id = ? order by dq.evaluated_at", [sid],
        ).fetchall()

        out["sources"].append({
            "source_id": sid, "connection_type": ctype, "domain": domain,
            "runs": [{"run_id": r[0], "status": r[1], "error": r[2], "started_at": str(r[3]),
                      "files_seen": r[4], "files_accepted": r[5], "files_quarantined": r[6],
                      "rows_loaded": r[7]} for r in runs],
            "latest": {"status": latest[1], "started_at": str(latest[3]),
                       "files_seen": latest[4], "files_accepted": latest[5],
                       "files_quarantined": latest[6], "rows_loaded": latest[7]} if latest else None,
            "bronze_rows": bronze_rows,
            "batches": [{"name": b[0], "size_kb": b[1], "row_count": b[2], "column_count": b[3],
                         "fqc_passed": b[4], "action": b[5], "arrival_time": str(b[6])} for b in batches],
            "dq": [{"rule_type": d[0], "columns": d[1], "severity": d[2], "passed": d[3],
                    "failed_row_count": d[4], "file_name": d[5]} for d in dq],
        })

    con.close()
    return out


def _status_of(source: dict) -> str:
    if not source["latest"]:
        return "unknown"
    if source["latest"]["status"] == "failed":
        return "failed"
    if source["latest"]["files_accepted"] > 0 and source["latest"]["files_quarantined"] == 0:
        return "clean"
    if source["latest"]["files_accepted"] > 0:
        return "partial"
    return "quarantined"


STATUS_LABEL = {"clean": "Loaded clean", "partial": "Partially loaded",
                "quarantined": "Fully quarantined", "failed": "Run failed", "unknown": "No runs"}


def render(data: dict) -> str:
    max_rows = max((s["bronze_rows"] for s in data["sources"]), default=1) or 1

    cards = []
    bars = []
    sections = []
    for s in data["sources"]:
        status = _status_of(s)
        latest = s["latest"] or {}
        cards.append(f"""
        <article class="card status-{status}">
          <div class="card-top">
            <span class="pill pill-{status}">{html.escape(STATUS_LABEL[status])}</span>
            <span class="conn-type">{html.escape(s["connection_type"])}</span>
          </div>
          <h3>{html.escape(s["source_id"])}</h3>
          <div class="card-domain">{html.escape(s["domain"])}</div>
          <div class="card-metrics">
            <div><span class="num">{s["bronze_rows"]:,}</span><span class="label">rows in bronze</span></div>
            <div><span class="num">{latest.get("files_accepted", 0)}</span><span class="label">batches accepted</span></div>
            <div><span class="num">{latest.get("files_quarantined", 0)}</span><span class="label">quarantined</span></div>
          </div>
        </article>""")

        bar_w = round(s["bronze_rows"] / max_rows * 100, 1)
        bars.append(f"""
        <div class="bar-row">
          <div class="bar-label">{html.escape(s["source_id"])}</div>
          <div class="bar-track"><div class="bar-fill status-{status}" style="width:{bar_w}%"></div></div>
          <div class="bar-value">{s["bronze_rows"]:,}</div>
        </div>""")

        batch_rows = "".join(f"""
          <tr>
            <td class="mono">{html.escape(b["name"])}</td>
            <td class="num-cell">{b["size_kb"]:.1f} KB</td>
            <td class="num-cell">{b["row_count"]:,}</td>
            <td class="num-cell">{b["column_count"]}</td>
            <td><span class="chip chip-{"pass" if b["fqc_passed"] else "fail"}">{"pass" if b["fqc_passed"] else "fail"}</span></td>
            <td><span class="chip chip-{"pass" if b["action"] == "loaded" else "fail"}">{html.escape(b["action"])}</span></td>
          </tr>""" for b in s["batches"]) or '<tr><td colspan="6" class="empty">No batches processed yet</td></tr>'

        dq_rows = "".join(f"""
          <tr>
            <td class="mono">{html.escape(d["file_name"] or "")}</td>
            <td class="mono">{html.escape(d["rule_type"])}</td>
            <td class="mono">{html.escape(d["columns"])}</td>
            <td><span class="sev sev-{d["severity"]}">{html.escape(d["severity"])}</span></td>
            <td><span class="chip chip-{"pass" if d["passed"] else "fail"}">{"pass" if d["passed"] else "fail"}</span></td>
            <td class="num-cell">{d["failed_row_count"]:,}</td>
          </tr>""" for d in s["dq"]) or '<tr><td colspan="6" class="empty">No quality rules evaluated yet</td></tr>'

        run_rows = "".join(f"""
          <tr>
            <td class="mono">{str(r["started_at"])[:19]}</td>
            <td><span class="chip chip-{"pass" if r["status"] == "completed" else "fail"}">{html.escape(r["status"])}</span></td>
            <td class="num-cell">{r["files_seen"]}</td>
            <td class="num-cell">{r["files_accepted"]}</td>
            <td class="num-cell">{r["files_quarantined"]}</td>
            <td class="num-cell">{r["rows_loaded"]:,}</td>
            <td class="mono error-msg">{html.escape(r["error"] or "")}</td>
          </tr>""" for r in s["runs"])

        sections.append(f"""
        <section class="detail" id="detail-{html.escape(s["source_id"])}">
          <h2>{html.escape(s["source_id"])} <span class="conn-type">({html.escape(s["connection_type"])})</span></h2>

          <h4>Run history</h4>
          <div class="table-wrap"><table>
            <thead><tr><th>Started</th><th>Status</th><th>Seen</th><th>Accepted</th><th>Quarantined</th><th>Rows</th><th>Error</th></tr></thead>
            <tbody>{run_rows}</tbody>
          </table></div>

          <h4>Batches / files</h4>
          <div class="table-wrap"><table>
            <thead><tr><th>Batch</th><th>Size</th><th>Rows</th><th>Cols</th><th>FQC</th><th>Outcome</th></tr></thead>
            <tbody>{batch_rows}</tbody>
          </table></div>

          <h4>Quality rule results</h4>
          <div class="table-wrap"><table>
            <thead><tr><th>Batch</th><th>Rule</th><th>Column(s)</th><th>Severity</th><th>Result</th><th>Failed rows</th></tr></thead>
            <tbody>{dq_rows}</tbody>
          </table></div>
        </section>""")

    nav_links = "".join(
        f'<a href="#detail-{html.escape(s["source_id"])}">{html.escape(s["source_id"])}</a>'
        for s in data["sources"]
    )

    title_suffix = "" if data["target"] == "duckdb" else f" — {data['target'].title()}"
    return f"""<title>Bronze Control Room{title_suffix}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {{
  --bg: #F6F7FB; --surface: #FFFFFF; --border: #E2E5F0;
  --text: #1B1E2B; --text-dim: #5B6076;
  --accent: #4A5FD9; --accent-dim: #E8EAFC;
  --success: #1B8A5A; --success-bg: #E4F5EC;
  --warn: #B7791F; --warn-bg: #FBF0DC;
  --error: #C4453D; --error-bg: #FBE7E5;
  --mono: "IBM Plex Mono", ui-monospace, monospace;
  --sans: "IBM Plex Sans", system-ui, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #12141C; --surface: #1A1D29; --border: #2A2E40;
    --text: #E9EAF2; --text-dim: #9498AC;
    --accent: #7C8CF0; --accent-dim: #232752;
    --success: #3FC783; --success-bg: #163227;
    --warn: #E0A94A; --warn-bg: #3A2E14;
    --error: #F0736B; --error-bg: #3A1B19;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #12141C; --surface: #1A1D29; --border: #2A2E40;
  --text: #E9EAF2; --text-dim: #9498AC;
  --accent: #7C8CF0; --accent-dim: #232752;
  --success: #3FC783; --success-bg: #163227;
  --warn: #E0A94A; --warn-bg: #3A2E14;
  --error: #F0736B; --error-bg: #3A1B19;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans);
  padding: 0 20px; padding-block: 32px 64px;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; }}
header {{ display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
h1 {{ font-family: var(--mono); font-size: 1.6rem; font-weight: 600; margin: 0; text-wrap: balance; }}
.meta {{ color: var(--text-dim); font-size: 0.85rem; font-family: var(--mono); }}
.subtitle {{ color: var(--text-dim); margin: 4px 0 28px; max-width: 65ch; }}
nav.jump {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 28px; }}
nav.jump a {{
  font-family: var(--mono); font-size: 0.8rem; color: var(--accent); text-decoration: none;
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; background: var(--surface);
}}
nav.jump a:hover {{ border-color: var(--accent); }}

.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 32px; }}
.card {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px;
  border-left: 3px solid var(--border);
}}
.card.status-clean {{ border-left-color: var(--success); }}
.card.status-partial {{ border-left-color: var(--warn); }}
.card.status-quarantined {{ border-left-color: var(--error); }}
.card.status-failed {{ border-left-color: var(--error); }}
.card-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
.card h3 {{ font-family: var(--mono); margin: 0 0 2px; font-size: 1.05rem; }}
.card-domain {{ color: var(--text-dim); font-size: 0.8rem; margin-bottom: 14px; }}
.conn-type {{
  font-family: var(--mono); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-dim);
}}
.card-metrics {{ display: flex; gap: 16px; }}
.card-metrics > div {{ display: flex; flex-direction: column; }}
.num {{ font-family: var(--mono); font-size: 1.3rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.label {{ color: var(--text-dim); font-size: 0.7rem; }}

.pill {{ font-size: 0.72rem; font-weight: 600; padding: 3px 9px; border-radius: 100px; font-family: var(--sans); }}
.pill-clean {{ background: var(--success-bg); color: var(--success); }}
.pill-partial {{ background: var(--warn-bg); color: var(--warn); }}
.pill-quarantined {{ background: var(--error-bg); color: var(--error); }}
.pill-failed {{ background: var(--error-bg); color: var(--error); }}
.pill-unknown {{ background: var(--border); color: var(--text-dim); }}

.chart {{
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px;
  margin-bottom: 36px;
}}
.chart h4 {{ margin: 0 0 16px; font-size: 0.85rem; color: var(--text-dim); font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }}
.bar-row {{ display: grid; grid-template-columns: 130px 1fr 70px; align-items: center; gap: 10px; margin-bottom: 10px; }}
.bar-label {{ font-family: var(--mono); font-size: 0.8rem; }}
.bar-track {{ height: 14px; background: var(--bg); border-radius: 4px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 4px; background: var(--accent); }}
.bar-fill.status-clean {{ background: var(--success); }}
.bar-fill.status-partial {{ background: var(--warn); }}
.bar-fill.status-quarantined, .bar-fill.status-failed {{ background: var(--error); }}
.bar-value {{ font-family: var(--mono); font-size: 0.8rem; text-align: right; font-variant-numeric: tabular-nums; }}

.detail {{ margin-bottom: 40px; }}
.detail h2 {{ font-family: var(--mono); font-size: 1.15rem; margin: 0 0 14px; }}
.detail h4 {{ font-size: 0.8rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; margin: 18px 0 8px; font-weight: 500; }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; background: var(--surface); }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
th {{ color: var(--text-dim); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }}
tr:last-child td {{ border-bottom: none; }}
.mono {{ font-family: var(--mono); font-size: 0.8rem; }}
.num-cell {{ font-family: var(--mono); font-variant-numeric: tabular-nums; text-align: right; }}
.empty {{ color: var(--text-dim); font-style: italic; text-align: center; }}
.error-msg {{ color: var(--error); max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

.chip {{ font-family: var(--mono); font-size: 0.72rem; padding: 2px 8px; border-radius: 4px; font-weight: 500; }}
.chip-pass {{ background: var(--success-bg); color: var(--success); }}
.chip-fail {{ background: var(--error-bg); color: var(--error); }}
.sev {{ font-family: var(--mono); font-size: 0.72rem; padding: 2px 8px; border-radius: 4px; font-weight: 500; text-transform: uppercase; }}
.sev-error {{ background: var(--error-bg); color: var(--error); }}
.sev-warn {{ background: var(--warn-bg); color: var(--warn); }}
</style>

<div class="wrap">
  <header>
    <h1>Bronze Control Room <span class="conn-type">-- {html.escape(data["target"])}</span></h1>
    <div class="meta">generated {html.escape(data["generated_at"])}</div>
  </header>
  <p class="subtitle">Landing-zone to bronze status for every source in the catalogue — file/batch checks, data-quality results, and what actually made it into <span class="mono">bronze.*</span>.</p>

  <nav class="jump">{nav_links}</nav>

  <div class="cards">{''.join(cards)}</div>

  <div class="chart">
    <h4>Rows landed in bronze, by source</h4>
    {''.join(bars)}
  </div>

  {''.join(sections)}
</div>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="duckdb", help="duckdb (default) or databricks")
    args = parser.parse_args()
    data = gather(args.target)
    out_path = OUT_PATH if args.target == "duckdb" else OUT_PATH.with_name(f"dashboard_{args.target}.html")
    out_path.write_text(render(data), encoding="utf-8")
    print(f"wrote {out_path} ({len(data['sources'])} sources, target={args.target})")


if __name__ == "__main__":
    main()
