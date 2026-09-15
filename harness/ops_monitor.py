"""Minimal implementation of the monitor process described in docs/alerting.md: polls
control.run_registry from OUTSIDE the platform and sends a Slack alert for a failed run. This
is the ops-monitor agent's job (.claude/agents/ops-monitor.md) in its simplest concrete form --
not a scheduler, just the one check + one alert, run on demand or via a real scheduler later.

Uses SLACK_WEBHOOK_URL (not the bot token) since a webhook needs no OAuth scopes for a one-way
post -- the original project decision, per docs/connect-slack.md: "This alone covers alerting."
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from emitters.sql_dialect import connect as sql_connect  # noqa: E402
import yaml  # noqa: E402


def _load_dotenv() -> None:
    env_path = REPO_ROOT / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _load_platform(target: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def latest_run(target: str, source_id: str) -> dict | None:
    platform = _load_platform(target)
    control = platform["storage"]["control"]
    con = sql_connect(target, platform)
    row = con.execute(
        f"select run_id, source_id, status, error_message, started_at, ended_at, "
        f"files_seen, files_accepted, files_quarantined, rows_loaded "
        f"from {control}.run_registry where source_id = ? order by started_at desc limit 1",
        [source_id],
    ).fetchone()
    con.close()
    if row is None:
        return None
    return {"run_id": row[0], "source_id": row[1], "status": row[2], "error_message": row[3],
            "started_at": row[4], "ended_at": row[5], "files_seen": row[6],
            "files_accepted": row[7], "files_quarantined": row[8], "rows_loaded": row[9]}


def send_slack_alert(webhook_url: str, run: dict, target: str) -> tuple[int, str]:
    if run["status"] == "failed":
        header = f":rotating_light: Jarvis bronze run FAILED -- `{run['source_id']}` ({target})"
        detail = f"*Error:* `{run['error_message']}`"
    else:
        header = f":warning: Jarvis bronze run had quarantined batches -- `{run['source_id']}` ({target})"
        detail = f"*Quarantined:* {run['files_quarantined']} of {run['files_seen']} files"

    payload = {
        "text": header,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text":
                f"run_id: `{run['run_id']}` | started: {run['started_at']} | "
                f"files_seen: {run['files_seen']} accepted: {run['files_accepted']} "
                f"quarantined: {run['files_quarantined']} rows_loaded: {run['rows_loaded']}"}]},
        ],
    }
    req = urllib.request.Request(
        webhook_url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", default="duckdb")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent, don't post to Slack")
    args = parser.parse_args()

    run = latest_run(args.target, args.source)
    if run is None:
        print(f"No runs found for source={args.source!r} target={args.target!r}")
        return

    print(f"Latest run: {run}")
    if run["status"] != "failed" and run["files_quarantined"] == 0:
        print("Run completed clean -- nothing to alert on.")
        return

    if args.dry_run:
        print("--dry-run: would send a Slack alert now.")
        return

    _load_dotenv()
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL not set in .env -- cannot send alert.", file=sys.stderr)
        sys.exit(1)

    status, body = send_slack_alert(webhook, run, args.target)
    print(f"Slack response: {status} {body}")


if __name__ == "__main__":
    main()
