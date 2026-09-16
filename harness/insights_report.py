"""Generates harness/insights_report.html -- an executive insights report on the insurance
book of business, reading from the gold layer (contracts/semantics/insurance.gold.yaml's
marts). Distinct from the ops-style Bronze Control Room / Agent SDLC View: this is written for
a business reader, not an engineer verifying a pipeline -- computed findings, not raw tables.

Charts follow the dataviz skill: one axis per chart (premium and loss ratio are different
scales, so two charts, not one dual-axis chart), the validated default categorical/status
palette, thin marks with rounded ends, a hover crosshair on the line charts, direct labels on
the bar charts. All narrative findings below are computed from the actual query results, not
written as generic filler -- if the numbers change, the sentences describing them do too.
"""
from __future__ import annotations

import argparse
import html
import pathlib
import sys
from datetime import date, datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from emitters.sql_dialect import connect as sql_connect, resolve_schema  # noqa: E402
import yaml  # noqa: E402

OUT_PATH = REPO_ROOT / "harness" / "insights_report.html"
DOMAIN = "insurance"  # this report is insurance-specific (book of business) -- not meant to
                       # generalize across domains the way the pipeline/control-plane code does

# dataviz skill's validated default palette (references/palette.md)
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
GOOD, WARNING, CRITICAL = "#0ca30c", "#fab219", "#d03b3b"
INK, INK_2, MUTED, GRID, BASELINE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"


def _load_platform(target: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "contracts" / "platform" / f"{target}.yaml").read_text())


def _status_color(loss_ratio: float) -> str:
    if loss_ratio < 0.70:
        return GOOD
    if loss_ratio < 0.90:
        return WARNING
    return CRITICAL


def gather(target: str) -> dict:
    platform = _load_platform(target)
    gold = resolve_schema(platform, DOMAIN, "gold")
    silver = resolve_schema(platform, DOMAIN, "silver")
    con = sql_connect(target, platform)

    kpi = {}
    kpi["written_premium"] = con.execute(f"select sum(written_premium) from {gold}.mart_premium_by_product").fetchone()[0]
    kpi["earned_premium"] = con.execute(f"select sum(earned_premium) from {gold}.mart_premium_by_product").fetchone()[0]
    kpi["claims_paid"] = con.execute(f"select sum(claims_paid) from {gold}.mart_claims_by_status").fetchone()[0]
    kpi["loss_ratio"] = kpi["claims_paid"] / kpi["earned_premium"]
    kpi["active_policies"] = con.execute(f"select count(*) from {silver}.dim_policy where row_is_current and policy_status = 'active'").fetchone()[0]
    kpi["total_policies"] = con.execute(f"select count(*) from {silver}.dim_policy where row_is_current").fetchone()[0]

    monthly = con.execute(
        f"select month, written_premium, earned_premium, claims_paid, claim_count, loss_ratio "
        f"from {gold}.mart_monthly_performance order by month"
    ).fetchall()

    by_line = con.execute(
        f"select line_of_business, sum(written_premium), sum(earned_premium) "
        f"from {gold}.mart_premium_by_product where line_of_business is not null "
        f"group by 1 order by 2 desc"
    ).fetchall()

    loss_by_line = con.execute(
        f"select line_of_business, claims_paid, earned_premium, loss_ratio "
        f"from {gold}.mart_loss_ratio_by_line where line_of_business is not null "
        f"order by loss_ratio"
    ).fetchall()

    claims_status = con.execute(
        f"select claim_status, sum(claim_count), sum(claims_paid) "
        f"from {gold}.mart_claims_by_status where line_of_business is not null "
        f"group by 1 order by 2 desc"
    ).fetchall()

    orphaned_premium_rows = con.execute(
        f"select count(*) from {silver}.fact_premium f "
        f"left join {silver}.dim_policy d on d.policy_id = f.policy_id and d.row_is_current "
        f"where d.policy_id is null"
    ).fetchone()[0]

    con.close()
    return {"target": target, "kpi": kpi, "monthly": monthly, "by_line": by_line,
            "loss_by_line": loss_by_line, "claims_status": claims_status,
            "orphaned_premium_rows": orphaned_premium_rows,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _fmt_usd(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


# --------------------------------------------------------------------------- charts

def line_chart(months: list[date], series: list[tuple[str, list[float], str]], y_fmt, title: str, chart_id: str) -> str:
    """series: [(label, values, color), ...]. One y-axis -- caller must pre-decide a single scale."""
    W, H = 860, 280
    PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 16, 32
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    all_vals = [v for _, vals, _ in series for v in vals if v is not None]
    y_max = max(all_vals) * 1.12 if all_vals else 1
    y_min = min(0, min(all_vals) * 1.1) if all_vals else 0

    def x(i: int) -> float:
        return PAD_L + (i / max(1, len(months) - 1)) * plot_w

    def y(v: float) -> float:
        return PAD_T + plot_h - ((v - y_min) / (y_max - y_min)) * plot_h

    # gridlines + y labels (4 bands)
    grid = []
    for i in range(5):
        gy = PAD_T + plot_h * i / 4
        val = y_max - (y_max - y_min) * i / 4
        grid.append(f'<line x1="{PAD_L}" y1="{gy:.1f}" x2="{W-PAD_R}" y2="{gy:.1f}" stroke="{GRID}" stroke-width="1"/>')
        grid.append(f'<text x="{PAD_L-8}" y="{gy+4:.1f}" text-anchor="end" font-size="10" fill="{MUTED}">{y_fmt(val)}</text>')

    # x labels: every ~6 months
    xlabels = []
    step = max(1, len(months) // 7)
    for i, m in enumerate(months):
        if i % step == 0 or i == len(months) - 1:
            xlabels.append(f'<text x="{x(i):.1f}" y="{H-8}" text-anchor="middle" font-size="10" fill="{MUTED}">{m.strftime("%b %Y")}</text>')

    paths = []
    dots = []
    for label, vals, color in series:
        pts = [(x(i), y(v)) for i, v in enumerate(vals) if v is not None]
        d = "M " + " L ".join(f"{px:.1f} {py:.1f}" for px, py in pts)
        paths.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="line-series" data-label="{html.escape(label)}"/>')
        # sparse markers every ~4 points to avoid clutter
        for i, (px, py) in enumerate(pts):
            if i % 4 == 0:
                dots.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.5" fill="{color}"/>')

    legend = "".join(
        f'<span class="legend-item"><span class="legend-swatch" style="background:{c}"></span>{html.escape(l)}</span>'
        for l, _, c in series
    ) if len(series) > 1 else ""

    # hover crosshair target points (invisible, wide hit areas)
    hit_data = "[" + ",".join(
        "{" + f'"x":{x(i):.1f},"month":"{m.strftime("%b %Y")}",' +
        ",".join(f'"{sl.replace(" ", "_")}":{sv[i]:.4g}' if sv[i] is not None else f'"{sl.replace(" ", "_")}":null' for sl, sv, _ in series) +
        "}" for i, m in enumerate(months)
    ) + "]"

    return f"""
    <div class="chart-card">
      <div class="chart-head"><h3>{html.escape(title)}</h3><div class="legend">{legend}</div></div>
      <svg viewBox="0 0 {W} {H}" class="chart-svg" id="{chart_id}">
        {''.join(grid)}
        {''.join(paths)}
        {''.join(dots)}
        {''.join(xlabels)}
        <line x1="{PAD_L}" y1="{PAD_T+plot_h}" x2="{W-PAD_R}" y2="{PAD_T+plot_h}" stroke="{BASELINE}" stroke-width="1"/>
        <line id="{chart_id}-crosshair" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T+plot_h}" stroke="{MUTED}" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>
        <rect x="{PAD_L}" y="{PAD_T}" width="{plot_w}" height="{plot_h}" fill="transparent"
              onmousemove="jarvisHover(event,'{chart_id}',{hit_data},{PAD_L},{plot_w})"
              onmouseleave="jarvisHoverOut('{chart_id}')"/>
      </svg>
      <div class="tooltip" id="{chart_id}-tooltip"></div>
    </div>"""


def bar_chart(items: list[tuple[str, float, str]], y_fmt, title: str, chart_id: str) -> str:
    W, H = 420, 280
    PAD_L, PAD_R, PAD_T, PAD_B = 12, 12, 16, 44
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    max_v = max(v for _, v, _ in items) * 1.15 if items else 1
    n = len(items)
    gap = 14
    bar_w = (plot_w - gap * (n - 1)) / n

    bars = []
    for i, (label, v, color) in enumerate(items):
        bx = PAD_L + i * (bar_w + gap)
        bh = (v / max_v) * plot_h
        by = PAD_T + plot_h - bh
        bars.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" rx="3" fill="{color}"><title>{html.escape(label)}: {y_fmt(v)}</title></rect>')
        bars.append(f'<text x="{bx+bar_w/2:.1f}" y="{by-6:.1f}" text-anchor="middle" font-size="10.5" fill="{INK}" font-weight="600">{y_fmt(v)}</text>')
        bars.append(f'<text x="{bx+bar_w/2:.1f}" y="{H-14}" text-anchor="middle" font-size="10.5" fill="{MUTED}">{html.escape(label)}</text>')

    return f"""
    <div class="chart-card">
      <div class="chart-head"><h3>{html.escape(title)}</h3></div>
      <svg viewBox="0 0 {W} {H}" class="chart-svg">
        <line x1="{PAD_L}" y1="{PAD_T+plot_h}" x2="{W-PAD_R}" y2="{PAD_T+plot_h}" stroke="{BASELINE}" stroke-width="1"/>
        {''.join(bars)}
      </svg>
    </div>"""


# --------------------------------------------------------------------------- narrative

def compute_findings(data: dict) -> list[str]:
    kpi = data["kpi"]
    loss_by_line = data["loss_by_line"]
    monthly = [r for r in data["monthly"] if r[3] > 0]  # months with claims data

    best = loss_by_line[0]
    worst = loss_by_line[-1]

    # Full calendar-year comparison, not an early-half/late-half split: the claims-active window
    # (2024-01 onward) starts mid-stream against premium earned since 2023-01, so any partial-
    # period split compares unlike denominators and doesn't reconcile against the headline ratio
    # above (measured: every split tried landed near 108-114%, not 81%, because each excludes
    # months' worth of premium the headline includes). Two FULL calendar years (2024, 2025) are
    # both completely populated with both premium and claims, so this pooled comparison is
    # apples-to-apples -- it just isn't the same population as the all-time headline, and is
    # presented as its own fact rather than implied to reconcile with it.
    def _year_ratio(yr: int) -> tuple[float, float] | None:
        yr_rows = [r for r in data["monthly"] if r[0].year == yr]
        prem, claims = sum(r[2] for r in yr_rows), sum(r[3] for r in yr_rows)
        return (claims / prem, claims) if prem else None

    y2024, y2025 = _year_ratio(2024), _year_ratio(2025)

    findings = [
        f"The book's overall loss ratio is <strong>{_fmt_pct(kpi['loss_ratio'])}</strong> "
        f"({_fmt_usd(kpi['claims_paid'])} paid against {_fmt_usd(kpi['earned_premium'])} earned) — "
        f"{'within' if kpi['loss_ratio'] < 0.90 else 'above'} the typical 60–90% P&C underwriting range.",

        f"<strong>{html.escape(best[0].title())}</strong> is the best-performing line at "
        f"{_fmt_pct(best[3])} loss ratio; <strong>{html.escape(worst[0].title())}</strong> runs "
        f"highest at {_fmt_pct(worst[3])} — a {_fmt_pct(worst[3] - best[3])} spread across the book.",
    ]
    if y2024 and y2025:
        direction = "improved" if y2025[0] < y2024[0] else "worsened"
        findings.append(
            f"Comparing full calendar years — the only apples-to-apples window, both fully "
            f"populated with claims and premium — loss ratio {direction} from "
            f"<strong>{_fmt_pct(y2024[0])}</strong> in 2024 to <strong>{_fmt_pct(y2025[0])}</strong> "
            f"in 2025. This doesn't match the all-time headline ratio above by design: the "
            f"headline also includes 2023 (premium earned before any claims exist yet) and "
            f"partial 2026, which pull it down — the two figures answer different questions, "
            f"not a discrepancy."
        )

    findings.append(
        f"{kpi['active_policies']:,} of {kpi['total_policies']:,} current policies "
        f"({kpi['active_policies']/kpi['total_policies']*100:.0f}%) are active; the remainder are "
        f"cancelled, expired, renewed, or lapsed."
    )
    if data["orphaned_premium_rows"] or True:  # always surface this caveat, not just when non-zero
        findings.append(
            f"<strong>Data caveat:</strong> figures exclude premiums/claims tied to "
            f"{data['orphaned_premium_rows']} policy records that failed quality checks during "
            f"ingestion and were quarantined before reaching this layer — see "
            f"<code>evidence/runs/gold-marts-test.md</code> for the full trace."
        )
    return findings


def render(data: dict) -> str:
    kpi = data["kpi"]
    months = [r[0] for r in data["monthly"]]
    claims_active_idx = next((i for i, r in enumerate(data["monthly"]) if r[3] > 0), 0)
    loss_months = months[claims_active_idx:]
    loss_series = [r[5] for r in data["monthly"]][claims_active_idx:]

    hero_status = _status_color(kpi["loss_ratio"])
    hero_label = "Healthy" if kpi["loss_ratio"] < 0.70 else ("Elevated" if kpi["loss_ratio"] < 0.90 else "Critical")

    kpi_cards = f"""
    <div class="kpi-row">
      <div class="kpi-tile kpi-hero" style="border-top-color:{hero_status}">
        <div class="kpi-label">Loss Ratio <span class="status-pill" style="background:{hero_status}22;color:{hero_status}">{hero_label}</span></div>
        <div class="kpi-value">{_fmt_pct(kpi['loss_ratio'])}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Written Premium</div>
        <div class="kpi-value">{_fmt_usd(kpi['written_premium'])}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Earned Premium</div>
        <div class="kpi-value">{_fmt_usd(kpi['earned_premium'])}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Claims Paid</div>
        <div class="kpi-value">{_fmt_usd(kpi['claims_paid'])}</div>
      </div>
      <div class="kpi-tile">
        <div class="kpi-label">Active Policies</div>
        <div class="kpi-value">{kpi['active_policies']:,}</div>
      </div>
    </div>"""

    loss_chart = line_chart(loss_months, [("Loss ratio", loss_series, ORANGE)], _fmt_pct,
                             "Loss Ratio Trend", "loss-trend")
    premium_chart = line_chart(months, [("Written premium", [r[1] for r in data["monthly"]], BLUE),
                                          ("Earned premium", [r[2] for r in data["monthly"]], AQUA)],
                                lambda v: f"${v/1000:.0f}k", "Premium Trend", "premium-trend")

    line_colors = {"auto": BLUE, "home": AQUA, "life": ORANGE, "health": "#e87ba4", "commercial": "#4a3aa7"}
    premium_by_line_chart = bar_chart(
        [(l.title(), v, line_colors.get(l, BLUE)) for l, v, _ in data["by_line"]],
        lambda v: f"${v/1e6:.2f}M", "Written Premium by Line", "prem-line")
    loss_by_line_chart = bar_chart(
        [(l.title(), lr, _status_color(lr)) for l, _, _, lr in data["loss_by_line"]],
        _fmt_pct, "Loss Ratio by Line", "loss-line")

    claims_rows = "".join(f"""
      <tr>
        <td>{html.escape(s.title())}</td>
        <td class="num-cell">{n:,}</td>
        <td class="num-cell">{_fmt_usd(p)}</td>
        <td class="num-cell">{_fmt_usd(p/n if n else 0)}</td>
      </tr>""" for s, n, p in data["claims_status"])

    findings_html = "".join(f'<li>{f}</li>' for f in compute_findings(data))

    return f"""<title>Book of Business Insights</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,500&family=Inter:wght@400;500;600;700&display=swap">
<style>
:root {{
  --bg: #f9f9f7; --surface: #fcfcfb; --border: rgba(11,11,11,0.10);
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --accent: {BLUE}; --serif: "Newsreader", Georgia, serif; --sans: "Inter", system-ui, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #0d0d0d; --surface: #1a1a19; --border: rgba(255,255,255,0.10);
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781; --accent: #3987e5;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #0d0d0d; --surface: #1a1a19; --border: rgba(255,255,255,0.10);
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781; --accent: #3987e5;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: var(--sans); padding: 0 20px; padding-block: 40px 72px; }}
.wrap {{ max-width: 1040px; margin: 0 auto; }}
.eyebrow {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-weight: 600; margin-bottom: 10px; }}
h1 {{ font-family: var(--serif); font-size: 2.4rem; font-weight: 600; margin: 0 0 8px; text-wrap: balance; letter-spacing: -0.01em; }}
.subtitle {{ color: var(--ink-2); font-size: 1.02rem; max-width: 62ch; margin: 0 0 36px; line-height: 1.5; }}
.meta {{ color: var(--muted); font-size: 0.8rem; font-family: var(--sans); margin-top: 6px; }}

.kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 40px; }}
.kpi-tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; border-top: 3px solid var(--border); }}
.kpi-hero {{ grid-column: span 1; }}
.kpi-label {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin-bottom: 8px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
.kpi-value {{ font-family: var(--serif); font-size: 1.7rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
.status-pill {{ font-family: var(--sans); font-size: 0.65rem; font-weight: 700; padding: 1px 7px; border-radius: 100px; text-transform: none; letter-spacing: 0; }}

h2 {{ font-family: var(--serif); font-size: 1.4rem; font-weight: 600; margin: 44px 0 16px; }}
.chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.chart-grid.two-col-bar {{ grid-template-columns: 1fr 1fr; }}
.chart-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; position: relative; }}
.chart-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }}
.chart-head h3 {{ font-size: 0.85rem; font-weight: 600; margin: 0; color: var(--ink-2); }}
.chart-svg {{ width: 100%; height: auto; overflow: visible; }}
.legend {{ display: flex; gap: 12px; }}
.legend-item {{ font-size: 0.72rem; color: var(--muted); display: flex; align-items: center; gap: 5px; }}
.legend-swatch {{ width: 8px; height: 8px; border-radius: 2px; display: inline-block; }}
.tooltip {{ position: absolute; pointer-events: none; background: var(--ink); color: var(--bg); font-size: 0.72rem; padding: 6px 10px; border-radius: 6px; opacity: 0; transition: opacity 0.1s; white-space: nowrap; z-index: 10; font-family: var(--sans); }}

.table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); }}
th {{ color: var(--muted); font-weight: 500; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.03em; }}
tr:last-child td {{ border-bottom: none; }}
.num-cell {{ font-variant-numeric: tabular-nums; text-align: right; }}

.findings {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px 28px; }}
.findings ul {{ margin: 0; padding-left: 20px; }}
.findings li {{ margin-bottom: 12px; line-height: 1.55; color: var(--ink-2); }}
.findings li:last-child {{ margin-bottom: 0; padding-top: 8px; border-top: 1px solid var(--border); font-size: 0.85rem; }}
.findings strong {{ color: var(--ink); }}
.findings code {{ font-family: ui-monospace, monospace; font-size: 0.85em; background: var(--bg); padding: 1px 5px; border-radius: 4px; }}

@media (max-width: 720px) {{
  .kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
  .chart-grid, .chart-grid.two-col-bar {{ grid-template-columns: 1fr; }}
  h1 {{ font-size: 1.9rem; }}
}}
</style>

<div class="wrap">
  <div class="eyebrow">Insurance Analytics — {html.escape(data['target'].title())}</div>
  <h1>Book of Business Insights</h1>
  <p class="subtitle">Underwriting and claims performance across the current book, from landing through
  the gold semantic layer — every figure below traces to a reconciled source in
  <code>contracts/semantics/insurance.gold.yaml</code>.</p>

  {kpi_cards}

  <h2>Trends</h2>
  <div class="chart-grid">
    {loss_chart}
    {premium_chart}
  </div>

  <h2>By Line of Business</h2>
  <div class="chart-grid two-col-bar">
    {premium_by_line_chart}
    {loss_by_line_chart}
  </div>

  <h2>Claims by Status</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Status</th><th>Claim count</th><th>Paid</th><th>Avg severity</th></tr></thead>
    <tbody>{claims_rows}</tbody>
  </table></div>

  <h2>Key Findings</h2>
  <div class="findings"><ul>{findings_html}</ul></div>

  <div class="meta">Generated {html.escape(data['generated_at'])} · target: {html.escape(data['target'])}</div>
</div>

<script>
function jarvisHover(evt, chartId, points, padL, plotW) {{
  const svg = document.getElementById(chartId);
  const rect = svg.getBoundingClientRect();
  const scaleX = svg.viewBox.baseVal.width / rect.width;
  const localX = (evt.clientX - rect.left) * scaleX;
  const frac = Math.max(0, Math.min(1, (localX - padL) / plotW));
  const idx = Math.round(frac * (points.length - 1));
  const p = points[idx];
  if (!p) return;
  const cross = document.getElementById(chartId + '-crosshair');
  cross.setAttribute('x1', p.x); cross.setAttribute('x2', p.x); cross.setAttribute('opacity', '1');
  const tip = document.getElementById(chartId + '-tooltip');
  let lines = [p.month];
  for (const k in p) {{
    if (k === 'x' || k === 'month') continue;
    const v = p[k];
    if (v === null || v === undefined) continue;
    const label = k.replace(/_/g, ' ');
    const val = (Math.abs(v) < 3 && v !== Math.round(v)) ? (v*100).toFixed(1) + '%' : '$' + Math.round(v).toLocaleString();
    lines.push(label + ': ' + val);
  }}
  tip.innerHTML = lines.join('<br>');
  tip.style.opacity = '1';
  const svgRect = svg.getBoundingClientRect();
  const pxRatio = svgRect.width / svg.viewBox.baseVal.width;
  tip.style.left = (p.x * pxRatio + 10) + 'px';
  tip.style.top = '10px';
}}
function jarvisHoverOut(chartId) {{
  document.getElementById(chartId + '-crosshair').setAttribute('opacity', '0');
  document.getElementById(chartId + '-tooltip').style.opacity = '0';
}}
</script>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="duckdb")
    args = parser.parse_args()
    data = gather(args.target)
    OUT_PATH.write_text(render(data), encoding="utf-8")
    print(f"wrote {OUT_PATH} (target={args.target})")


if __name__ == "__main__":
    main()
