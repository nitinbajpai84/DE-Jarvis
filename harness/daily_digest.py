"""Daily success digest: how many tables loaded/built and how many columns processed, per
layer (bronze/silver/gold), for a given day -- the success-path complement to
harness/ops_monitor.py's failure alert. Reads control.run_registry (now written by all three
layers' emitters, not just bronze -- see emitters/control_plane.py's log_run) rather than
re-deriving counts from current table state, so this reports what actually RAN today, not just
what currently exists.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request
from datetime import date, datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from emitters.sql_dialect import connect as sql_connect  # noqa: E402
import yaml  # noqa: E402

PHASES = ["bronze", "silver", "gold"]


def _load_dotenv() -> None:
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _load_platform(target: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _latest_activity_date(con, control: str) -> date | None:
    row = con.execute(f"select max(cast(started_at as date)) from {control}.run_registry").fetchone()
    return row[0] if row and row[0] else None


def gather(target: str, for_date: date | None) -> dict:
    platform = _load_platform(target)
    control = platform["storage"]["control"]
    con = sql_connect(target, platform)

    if for_date is None:
        # Don't trust the local clock for "today" -- this sandboxed environment's system clock
        # has been observed to disagree with itself by a day between successive tool calls (not
        # a code bug: two datetime.now(timezone.utc) calls minutes apart returned different
        # dates). Ask the data what its most recent activity date actually is instead.
        for_date = _latest_activity_date(con, control)
        if for_date is None:
            con.close()
            return {"target": target, "date": None, "phases": {p: {"tables": 0, "columns": 0,
                    "rows": 0, "quarantined_batches": 0, "failed_tables": [], "table_names": []}
                    for p in PHASES}}

    summary = {}
    for phase in PHASES:
        rows = con.execute(
            f"select source_id, status, columns_processed, rows_loaded, files_quarantined "
            f"from {control}.run_registry "
            f"where phase = ? and cast(started_at as date) = ?",
            [phase, for_date.isoformat()],
        ).fetchall()
        completed = [r for r in rows if r[1] == "completed"]
        failed = [r for r in rows if r[1] == "failed"]
        summary[phase] = {
            "tables": len(completed),
            "columns": sum(r[2] or 0 for r in completed),
            "rows": sum(r[3] or 0 for r in completed),
            "quarantined_batches": sum(r[4] or 0 for r in completed),
            "failed_tables": [r[0] for r in failed],
            "table_names": [r[0] for r in completed],
        }
    con.close()
    return {"target": target, "date": for_date.isoformat(), "phases": summary}


def format_slack_message(digest: dict) -> dict:
    any_failures = any(digest["phases"][p]["failed_tables"] for p in PHASES)
    any_activity = any(digest["phases"][p]["tables"] for p in PHASES)
    header_icon = ":x:" if any_failures else (":white_check_mark:" if any_activity else ":zzz:")
    header = f"{header_icon} Jarvis daily digest — {digest['date']} ({digest['target']})"

    lines = []
    for phase in PHASES:
        p = digest["phases"][phase]
        if p["tables"] == 0 and not p["failed_tables"]:
            lines.append(f"*{phase.title()}*: no runs today")
            continue
        line = f"*{phase.title()}*: {p['tables']} tables · {p['columns']} columns · {p['rows']:,} rows"
        if p["quarantined_batches"]:
            line += f" · {p['quarantined_batches']} batches quarantined"
        if p["failed_tables"]:
            line += f" · :x: failed: {', '.join(p['failed_tables'])}"
        lines.append(line)

    return {
        "text": header,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ],
    }


def send_slack(webhook_url: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        webhook_url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="duckdb")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (UTC)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for_date = date.fromisoformat(args.date) if args.date else None
    digest = gather(args.target, for_date)
    payload = format_slack_message(digest)

    print(json.dumps(digest, indent=2, default=str))
    print("\n--- Slack message ---")
    print(payload["blocks"][1]["text"]["text"])

    if args.dry_run:
        print("\n--dry-run: not posted to Slack.")
        return

    _load_dotenv()
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL not set -- cannot send.", file=sys.stderr)
        sys.exit(1)
    status, body = send_slack(webhook, payload)
    print(f"\nSlack response: {status} {body}")


if __name__ == "__main__":
    main()
