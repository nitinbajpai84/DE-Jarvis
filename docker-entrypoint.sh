#!/bin/sh
# Wires a Railway persistent volume (mounted at $JARVIS_DATA_ROOT, e.g. /data) into the paths
# every Python module in this repo already hardcodes relative to REPO_ROOT -- no application
# code changes, no new path-config layer to keep in sync. Every dynamic/writable path becomes
# a symlink into the volume; everything else (source code, docs/templates/) stays exactly
# where the image built it. Idempotent -- safe to run on every container start, not just the
# first.
#
# Local dev (JARVIS_DATA_ROOT unset) skips all of this and runs exactly as it always has.
set -eu

if [ -n "${JARVIS_DATA_ROOT:-}" ]; then
  DATA="$JARVIS_DATA_ROOT"
  mkdir -p "$DATA/harness_state/landing" "$DATA/harness_state/quarantine" \
           "$DATA/harness_state/agent_run_logs" "$DATA/uploads"

  # contracts/ is the one directory that's both git-tracked (insurance, asset_management --
  # already onboarded, ship with every image) and runtime-written (a future Freeze-gate
  # approval writes a new domain's contracts here) -- seed it from the image once, then let
  # the volume be authoritative from here on, so a re-deploy never overwrites what a real
  # agent run wrote.
  if [ ! -d "$DATA/contracts" ]; then
    cp -r /app/contracts "$DATA/contracts"
  fi
  rm -rf /app/contracts
  ln -s "$DATA/contracts" /app/contracts

  # harness/landing/ is gitignored+dockerignored (real per-customer runtime state, not source),
  # so a fresh volume starts with nothing for Step 01 discovery or Step 03 build to find --
  # confirmed live: every discovery test_connection against the deployed backend failed with
  # "no files match" because there was genuinely nothing there. Seed it once from
  # harness/seed_landing/ (the same historical files the local/demo environment has always
  # used, deliberately kept out of .gitignore/.dockerignore so they DO ship with the image),
  # exactly the same "seed once, volume authoritative after that" pattern already used for
  # contracts/ above -- a real agent run or a real file drop then owns it from here on, and a
  # re-deploy never overwrites what happened since.
  if [ ! -d "$DATA/harness_state/landing" ] || [ -z "$(ls -A "$DATA/harness_state/landing" 2>/dev/null)" ]; then
    mkdir -p "$DATA/harness_state/landing"
    cp -r /app/harness/seed_landing/. "$DATA/harness_state/landing/"
  fi

  # harness/ mixes source (.py) with state (the duckdb file, checkpoints, logs) -- symlink
  # only the state, leave the source where the image put it.
  ln -sfn "$DATA/harness_state/landing" /app/harness/landing
  ln -sfn "$DATA/harness_state/quarantine" /app/harness/quarantine
  ln -sfn "$DATA/harness_state/agent_run_logs" /app/harness/agent_run_logs
  ln -sf "$DATA/harness_state/jarvis.duckdb" /app/harness/jarvis.duckdb
  ln -sf "$DATA/harness_state/agent_checkpoints.sqlite" /app/harness/agent_checkpoints.sqlite
  ln -sf "$DATA/harness_state/sdlc_log_failures.log" /app/harness/sdlc_log_failures.log

  rm -rf /app/webapp/uploads
  ln -sfn "$DATA/uploads" /app/webapp/uploads
fi

exec uvicorn webapp.backend.main:app --host 0.0.0.0 --port "${PORT:-8010}"
