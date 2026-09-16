"""Thin CLI entry for the bronze loader, matching harness/seed/generate.py's convention of
one runnable script per concern. The actual algorithm lives in emitters/bronze_loader.py.

Also fires the ops-monitor Slack alert (harness/ops_monitor.py) when the run it just kicked
off failed or quarantined anything -- previously that check only ran when someone remembered
to invoke ops_monitor.py separately afterward, so a real quarantine event could sit in
control.run_registry with no Slack message ever sent. Kept as a post-run check here (not
folded into bronze_loader.run() itself) so the loader stays free of alerting/HTTP concerns --
same "polls run_registry from outside the platform" design ops_monitor.py's own docstring
describes, just no longer requiring a human to remember the second command.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from emitters.bronze_loader import run
from harness.ops_monitor import _load_dotenv, latest_run, send_slack_alert

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="source_id, e.g. 'orders'")
    parser.add_argument("--target", default="duckdb", help="duckdb (default) or databricks")
    parser.add_argument("--no-alert", action="store_true", help="skip the post-run Slack alert check")
    args = parser.parse_args()
    summary = run(args.source, args.target)
    for k, v in summary.items():
        print(f"{k:<18} {v}")

    if not args.no_alert:
        run_info = latest_run(args.target, args.source)
        if run_info and (run_info["status"] == "failed" or run_info["files_quarantined"] > 0):
            _load_dotenv()
            webhook = os.environ.get("SLACK_WEBHOOK_URL")
            if webhook:
                status, body = send_slack_alert(webhook, run_info, args.target)
                print(f"slack_alert       sent ({status})")
            else:
                print("slack_alert       SLACK_WEBHOOK_URL not set -- skipped", file=sys.stderr)
