"""Thin CLI entry for the bronze loader, matching harness/seed/generate.py's convention of
one runnable script per concern. The actual algorithm lives in emitters/bronze_loader.py.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from emitters.bronze_loader import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="source_id, e.g. 'orders'")
    parser.add_argument("--target", default="duckdb", help="duckdb (default) or databricks")
    args = parser.parse_args()
    summary = run(args.source, args.target)
    for k, v in summary.items():
        print(f"{k:<18} {v}")
