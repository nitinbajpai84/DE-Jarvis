"""Generates harness/agent_dashboard.html: which of the 10 .claude/agents own each stage of
the Agentic SDLC (docs/agentic-sdlc.md), and what evidence actually exists for each phase run
so far -- gate records, specs, ADRs, test runs, reviews. Honest about scope: this session ran
mostly as one continuous Claude Code session performing every role directly, not 10 independently
invoked agents -- the one genuine exception (a separately spawned subagent with no prior
context, for P1's Stage 8 Review) is called out explicitly, not blurred into the rest.
"""
from __future__ import annotations

import html
import pathlib
import re
import sys
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from agents_loader import _parse, AGENT_DIR  # noqa: E402

OUT_PATH = REPO_ROOT / "harness" / "agent_dashboard.html"

STAGES = [
    (1, "Intent", "program-manager"),
    (2, "Requirement Analysis", "business-analyst"),
    (3, "Codebase Discovery", "program-manager"),
    (4, "Design", "solution-architect + data-architect"),
    (5, "Plan", "program-manager"),
    (6, "Implementation", "de-bronze / de-silver / de-gold"),
    (7, "Testing & Validation", "test-manager"),
    (8, "Review", "reviewer"),
    (9, "Impact Analysis", "solution-architect"),
    (10, "Evidence", "evidence-agent"),
]
GATES = {2: "G1 Intent Gate", 5: "G2 Architecture Gate", 9: "G3 Validation Gate"}

# Hand-curated against the actual files in evidence/ and specs/ -- not auto-discovered, since
# the naming isn't structured enough yet to map reliably and a wrong auto-guess here is worse
# than a short, honestly-maintained list.
PHASES = [
    {
        "name": "P1 -- Source to Bronze (orders)",
        "note": "Full 10-stage SDLC followed, including two human-directed gate records and a "
                "genuinely independent review (fresh subagent, no prior session context).",
        "stages": {
            1: ["evidence/gates/P1-intent.md"],
            2: ["specs/P1/requirements.md"],
            3: ["specs/P1/discovery.md"],
            4: ["specs/P1/design.md", "evidence/decisions/ADR-001-p1-ingestion-approach.md"],
            5: ["specs/P1/plan.md", "evidence/gates/P1-architecture.md"],
            6: ["emitters/bronze_loader.py", "emitters/control_plane.py"],
            7: ["evidence/runs/P1-bronze-run.md"],
            8: ["evidence/decisions/P1-review.md"],
            9: ["evidence/decisions/P1-impact-and-evidence.md"],
            10: ["evidence/decisions/P1-impact-and-evidence.md"],
        },
    },
    {
        "name": "Catalogue-driven onboarding + multi-source-type bronze (insurance model)",
        "note": "Iterated directly against user feedback in-session rather than through "
                "formal gate documents -- no evidence/gates/ or specs/ records for this pass. "
                "Real bugs found and fixed are recorded in ADR-001's resolution note and commit "
                "messages instead of a separate stage-by-stage trail.",
        "stages": {
            6: ["emitters/sql_dialect.py", "emitters/catalogue_compiler.py",
                "harness/seed/generate_insurance.py"],
            9: ["evidence/decisions/ADR-001-p1-ingestion-approach.md (Resolution section)"],
        },
    },
    {
        "name": "Bronze -> Silver: SCD1/SCD2 conform",
        "note": "Implementation + testing done directly; no separate gate documents for this "
                "pass either. Testing is real and evidenced, just not preceded by a written "
                "Intent/Requirements/Design stage the way P1 was.",
        "stages": {
            6: ["emitters/silver_transform.py", "emitters/silver_model_compiler.py",
                "contracts/models/insurance.model.yaml"],
            7: ["evidence/runs/silver-scd-test.md"],
        },
    },
]


def _file_exists(rel: str) -> bool:
    path_part = rel.split(" (")[0]
    return (REPO_ROOT / path_part).exists()


def render() -> str:
    agents = [_parse(p) for p in sorted(AGENT_DIR.glob("*.md"))]

    agent_cards = "".join(f"""
    <article class="acard">
      <div class="acard-top">
        <h3>{html.escape(a['name'])}</h3>
        <span class="pill pill-{'sonnet' if 'sonnet' in a['model'] else 'haiku'}">{html.escape(a['model'])}</span>
      </div>
      <p>{html.escape(a['description'])}</p>
    </article>""" for a in agents)

    stage_rows = "".join(f"""
    <tr>
      <td class="num-cell">{n}</td>
      <td>{html.escape(name)}{' <span class="gate">' + html.escape(GATES[n]) + '</span>' if n in GATES else ''}</td>
      <td class="mono">{html.escape(owner)}</td>
    </tr>""" for n, name, owner in STAGES)

    phase_sections = []
    for phase in PHASES:
        rows = []
        for n, name, owner in STAGES:
            artifacts = phase["stages"].get(n, [])
            if not artifacts:
                rows.append(f"""
        <tr class="stage-empty">
          <td class="num-cell">{n}</td><td>{html.escape(name)}</td><td class="mono">{html.escape(owner)}</td>
          <td class="empty">no artifact for this pass</td>
        </tr>""")
                continue
            links = "<br>".join(
                f'<span class="chip chip-{"pass" if _file_exists(a) else "fail"}">{"found" if _file_exists(a) else "missing"}</span> '
                f'<span class="mono">{html.escape(a)}</span>'
                for a in artifacts
            )
            rows.append(f"""
        <tr>
          <td class="num-cell">{n}</td><td>{html.escape(name)}</td><td class="mono">{html.escape(owner)}</td>
          <td>{links}</td>
        </tr>""")
        phase_sections.append(f"""
    <section class="detail">
      <h2>{html.escape(phase['name'])}</h2>
      <p class="subtitle">{html.escape(phase['note'])}</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Stage</th><th>Name</th><th>Owner agent</th><th>Artifact</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </section>""")

    return f"""<title>Agent SDLC View</title>
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
body {{ margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans); padding: 0 20px; padding-block: 32px 64px; }}
.wrap {{ max-width: 1080px; margin: 0 auto; }}
h1 {{ font-family: var(--mono); font-size: 1.6rem; font-weight: 600; margin: 0 0 6px; text-wrap: balance; }}
.subtitle {{ color: var(--text-dim); margin: 4px 0 28px; max-width: 70ch; }}
h2 {{ font-family: var(--mono); font-size: 1.15rem; margin: 0 0 6px; }}
h3 {{ font-family: var(--mono); font-size: 0.95rem; margin: 0; }}
.section-label {{ font-size: 0.8rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; margin: 32px 0 12px; font-weight: 500; }}
.agents {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-bottom: 12px; }}
.acard {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
.acard-top {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 6px; }}
.acard p {{ margin: 0; font-size: 0.8rem; color: var(--text-dim); line-height: 1.4; }}
.pill {{ font-family: var(--mono); font-size: 0.65rem; padding: 2px 8px; border-radius: 100px; white-space: nowrap; }}
.pill-sonnet {{ background: var(--accent-dim); color: var(--accent); }}
.pill-haiku {{ background: var(--warn-bg); color: var(--warn); }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; background: var(--surface); }}
th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
th {{ color: var(--text-dim); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }}
tr:last-child td {{ border-bottom: none; }}
.mono {{ font-family: var(--mono); font-size: 0.8rem; }}
.num-cell {{ font-family: var(--mono); text-align: center; width: 40px; }}
.gate {{ font-family: var(--mono); font-size: 0.7rem; background: var(--accent-dim); color: var(--accent); padding: 2px 8px; border-radius: 4px; margin-left: 8px; }}
.chip {{ font-family: var(--mono); font-size: 0.7rem; padding: 1px 7px; border-radius: 4px; font-weight: 500; }}
.chip-pass {{ background: var(--success-bg); color: var(--success); }}
.chip-fail {{ background: var(--error-bg); color: var(--error); }}
.empty {{ color: var(--text-dim); font-style: italic; }}
.stage-empty {{ opacity: 0.55; }}
.detail {{ margin-bottom: 36px; }}
</style>

<div class="wrap">
  <h1>Agent / SDLC View</h1>
  <p class="subtitle">Which agent owns each stage of the Agentic SDLC (docs/agentic-sdlc.md), and what
  evidence actually exists for each phase run so far. This session mostly ran as one continuous
  Claude Code session performing every role directly -- not 10 independently invoked agents.
  The one genuine exception (a separately spawned subagent with no prior context, for P1's
  Stage 8 Review) is called out explicitly below rather than blurred into the rest.</p>

  <div class="section-label">The team -- .claude/agents/*.md</div>
  <div class="agents">{agent_cards}</div>

  <div class="section-label">The 10-stage SDLC (docs/agentic-sdlc.md)</div>
  <div class="table-wrap"><table>
    <thead><tr><th>#</th><th>Stage</th><th>Owner agent</th></tr></thead>
    <tbody>{stage_rows}</tbody>
  </table></div>

  <div class="section-label">What's actually been run, stage by stage</div>
  {''.join(phase_sections)}
</div>
"""


def main() -> None:
    OUT_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
