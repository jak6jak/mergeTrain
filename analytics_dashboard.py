import duckdb
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

con = duckdb.connect("godot_prs.duckdb")

# ── Palette ──────────────────────────────────────────────────────────────────
BG     = "#0d1117"
CARD   = "#161b22"
BORDER = "#30363d"
BLUE   = "#478CBF"
CYAN   = "#79C0FF"
GREEN  = "#3FB950"
ORANGE = "#FF8C42"
RED    = "#F85149"
PURPLE = "#BC8CFF"
YELLOW = "#E3B341"
TEXT   = "#e6edf3"
MUTED  = "#8b949e"

FONT = dict(family="'Inter','Segoe UI',system-ui,sans-serif", color=TEXT, size=13)

def base_layout(t=70, **kw):
    return dict(
        paper_bgcolor=CARD,
        plot_bgcolor=CARD,
        font=FONT,
        margin=dict(l=20, r=20, t=t, b=20),
        **kw,
    )

# ── 1. PR Volume by Year ──────────────────────────────────────────────────────
yr = con.execute("""
    SELECT YEAR(created_at) yr,
           COUNT(*) total,
           SUM(CASE WHEN merged_at IS NOT NULL THEN 1 ELSE 0 END) merged,
           SUM(CASE WHEN state='open' THEN 1 ELSE 0 END) still_open
    FROM pull_requests
    GROUP BY yr ORDER BY yr
""").df()
yr["closed_unmerged"] = yr["total"] - yr["merged"] - yr["still_open"]
yr["merge_rate"]      = 100 * yr["merged"] / yr["total"]

fig1 = go.Figure()
fig1.add_trace(go.Bar(x=yr["yr"], y=yr["merged"],         name="Merged",           marker_color=GREEN,  hovertemplate="%{y:,} merged<extra></extra>"))
fig1.add_trace(go.Bar(x=yr["yr"], y=yr["closed_unmerged"],name="Closed (unmerged)", marker_color=RED,    hovertemplate="%{y:,} closed unmerged<extra></extra>"))
fig1.add_trace(go.Bar(x=yr["yr"], y=yr["still_open"],     name="Still Open",        marker_color=YELLOW, hovertemplate="%{y:,} still open<extra></extra>"))
fig1.add_trace(go.Scatter(
    x=yr["yr"], y=yr["merge_rate"], name="Merge Rate %", yaxis="y2",
    line=dict(color=CYAN, width=2.5, dash="dot"), mode="lines+markers",
    marker=dict(size=5), hovertemplate="%{y:.1f}% merge rate<extra></extra>",
))
fig1.update_layout(**base_layout(
    t=100,
    title=dict(text="PR Submissions & Outcomes by Year", font=dict(size=16, color=TEXT), x=0),
    barmode="stack",
    legend=dict(orientation="h", x=0, y=1.13, bgcolor="rgba(0,0,0,0)"),
    xaxis=dict(dtick=1, gridcolor=BORDER),
    yaxis=dict(gridcolor=BORDER, title="Pull Requests"),
    yaxis2=dict(overlaying="y", side="right", range=[0, 110], ticksuffix="%",
                gridcolor="rgba(0,0,0,0)", title="Merge Rate"),
    hovermode="x unified",
))

# ── 2. Time to Merge ──────────────────────────────────────────────────────────
ttm = con.execute("""
    SELECT
        CASE
            WHEN days = 0           THEN 'Same day'
            WHEN days <= 3          THEN '1–3 days'
            WHEN days <= 7          THEN '4–7 days'
            WHEN days <= 30         THEN '1–4 weeks'
            WHEN days <= 90         THEN '1–3 months'
            WHEN days <= 365        THEN '3–12 months'
            ELSE                         '1+ year'
        END AS bucket,
        COUNT(*) AS cnt,
        CASE
            WHEN days = 0    THEN 0 WHEN days <= 3   THEN 1
            WHEN days <= 7   THEN 2 WHEN days <= 30  THEN 3
            WHEN days <= 90  THEN 4 WHEN days <= 365 THEN 5
            ELSE 6
        END AS ord
    FROM (SELECT DATEDIFF('day', created_at, merged_at) AS days
          FROM pull_requests WHERE merged_at IS NOT NULL)
    GROUP BY bucket, ord ORDER BY ord
""").df()
ttm["pct"] = 100 * ttm["cnt"] / ttm["cnt"].sum()
ttm_colors = [GREEN, "#4ade80", "#86efac", YELLOW, ORANGE, RED, "#7f1d1d"]

fig2 = go.Figure(go.Bar(
    x=ttm["cnt"], y=ttm["bucket"],
    orientation="h",
    marker_color=ttm_colors[:len(ttm)],
    text=[f"{p:.1f}%" for p in ttm["pct"]],
    textposition="outside",
    textfont=dict(color=TEXT, size=12),
    hovertemplate="<b>%{y}</b><br>%{x:,} PRs (%{text})<extra></extra>",
    width=0.65,
))
fig2.update_layout(**base_layout(
    title=dict(text="Time-to-Merge Distribution  (42,188 merged PRs)", font=dict(size=16, color=TEXT), x=0),
    xaxis=dict(gridcolor=BORDER, title="Number of PRs"),
    yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"),
    showlegend=False,
))
fig2.update_layout(margin=dict(l=20, r=80, t=50, b=20))

# ── 3. Contributor Funnel ─────────────────────────────────────────────────────
assoc = con.execute("""
    SELECT author_association,
           COUNT(*) total,
           SUM(CASE WHEN merged_at IS NOT NULL THEN 1 ELSE 0 END) merged
    FROM pull_requests GROUP BY author_association ORDER BY total DESC
""").df()
assoc["merge_rate"]  = 100 * assoc["merged"] / assoc["total"]
assoc["not_merged"]  = assoc["total"] - assoc["merged"]
assoc["label"] = assoc["author_association"].map({
    "CONTRIBUTOR": "Contributors", "MEMBER": "Core Members",
    "NONE": "First-timers", "COLLABORATOR": "Collaborators",
})

fig3 = make_subplots(rows=1, cols=2,
    subplot_titles=["Volume by Contributor Type", "Merge Rate by Type"],
    column_widths=[0.55, 0.45])
fig3.add_trace(go.Bar(x=assoc["label"], y=assoc["merged"],     name="Merged",     marker_color=GREEN), row=1, col=1)
fig3.add_trace(go.Bar(x=assoc["label"], y=assoc["not_merged"], name="Not Merged", marker_color=RED),   row=1, col=1)
fig3.add_trace(go.Bar(
    x=assoc["label"], y=assoc["merge_rate"],
    name="Merge %", showlegend=False,
    marker_color=[GREEN, BLUE, ORANGE, PURPLE],
    text=[f"{r:.1f}%" for r in assoc["merge_rate"]],
    textposition="outside",
    hovertemplate="<b>%{x}</b><br>%{y:.1f}% merge rate<extra></extra>",
), row=1, col=2)
fig3.update_layout(**base_layout(
    t=100,
    title=dict(text="The Contributor Funnel", font=dict(size=16, color=TEXT), x=0),
    barmode="stack",
    legend=dict(orientation="h", x=0, y=1.15, bgcolor="rgba(0,0,0,0)"),
    yaxis=dict(gridcolor=BORDER),
    yaxis2=dict(gridcolor=BORDER, range=[0, 112], ticksuffix="%"),
    xaxis=dict(gridcolor="rgba(0,0,0,0)"),
    xaxis2=dict(gridcolor="rgba(0,0,0,0)"),
))
fig3.update_annotations(font_color=MUTED)

# ── 4. Review Economy ─────────────────────────────────────────────────────────
rev_top = con.execute("""
    SELECT reviewer_login,
           COUNT(*) reviews,
           SUM(CASE WHEN state='APPROVED'           THEN 1 ELSE 0 END) approvals,
           SUM(CASE WHEN state='CHANGES_REQUESTED'  THEN 1 ELSE 0 END) changes_req
    FROM pr_reviews GROUP BY reviewer_login ORDER BY reviews DESC LIMIT 15
""").df()

rev_states = con.execute("SELECT state, COUNT(*) cnt FROM pr_reviews GROUP BY state").df()

fig4 = make_subplots(rows=1, cols=2,
    subplot_titles=["Top 15 Reviewers", "Review Verdicts"],
    column_widths=[0.65, 0.35],
    specs=[[{"type": "bar"}, {"type": "pie"}]])

fig4.add_trace(go.Bar(
    x=rev_top["reviews"], y=rev_top["reviewer_login"], orientation="h",
    name="Reviews", marker_color=BLUE,
    hovertemplate="<b>%{y}</b><br>%{x} reviews<extra></extra>",
), row=1, col=1)
fig4.add_trace(go.Bar(
    x=rev_top["approvals"], y=rev_top["reviewer_login"], orientation="h",
    name="Approvals", marker_color=GREEN,
    hovertemplate="<b>%{y}</b><br>%{x} approvals<extra></extra>",
), row=1, col=1)

state_colors = {"COMMENTED": BLUE, "APPROVED": GREEN, "CHANGES_REQUESTED": ORANGE, "DISMISSED": MUTED}
fig4.add_trace(go.Pie(
    labels=rev_states["state"], values=rev_states["cnt"],
    marker_colors=[state_colors.get(s, MUTED) for s in rev_states["state"]],
    hole=0.55,
    textinfo="label+percent", textfont=dict(size=11),
    hovertemplate="<b>%{label}</b><br>%{value:,} (%{percent})<extra></extra>",
), row=1, col=2)
fig4.update_layout(**base_layout(
    t=100,
    title=dict(text="The Review Economy", font=dict(size=16, color=TEXT), x=0),
    barmode="overlay",
    legend=dict(orientation="h", x=0, y=1.15, bgcolor="rgba(0,0,0,0)"),
    xaxis=dict(gridcolor=BORDER),
    yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"),
))
fig4.update_annotations(font_color=MUTED)

# ── 5. Community Wishlist ─────────────────────────────────────────────────────
wish = con.execute("""
    SELECT number,
           CASE WHEN LENGTH(title) > 55 THEN LEFT(title, 52) || '…' ELSE title END title_short,
           title, reactions_total, reactions_plus1, comments
    FROM pull_requests
    WHERE state='open' AND merged_at IS NULL AND reactions_total > 20
    ORDER BY reactions_total DESC LIMIT 12
""").df().sort_values("reactions_total")

fig5 = go.Figure(go.Bar(
    x=wish["reactions_total"], y=wish["title_short"],
    orientation="h",
    marker=dict(color=wish["reactions_total"],
                colorscale=[[0, BLUE], [1, PURPLE]], showscale=False),
    text=[f"#{n}" for n in wish["number"]],
    textposition="outside",
    textfont=dict(color=MUTED, size=10),
    customdata=list(zip(wish["number"], wish["reactions_plus1"], wish["comments"], wish["title"])),
    hovertemplate="<b>#%{customdata[0]}</b><br>%{customdata[3]}<br>👍 %{customdata[1]}  💬 %{customdata[2]}<extra></extra>",
    width=0.7,
))
fig5.update_layout(**base_layout(
    title=dict(text="Community Wishlist — Most-Wanted Open PRs", font=dict(size=16, color=TEXT), x=0),
    xaxis=dict(gridcolor=BORDER, title="Total Reactions"),
    yaxis=dict(gridcolor="rgba(0,0,0,0)"),
    showlegend=False,
    height=480,
))
fig5.update_layout(margin=dict(l=20, r=60, t=50, b=20))

# ── 6. Label Treemap ──────────────────────────────────────────────────────────
lbl = con.execute("""
    SELECT label_name, COUNT(*) cnt
    FROM pr_labels GROUP BY label_name ORDER BY cnt DESC LIMIT 35
""").df()

def label_cat(name):
    if name.startswith("topic:"):     return "Topic"
    if name.startswith("platform:"):  return "Platform"
    if name.startswith("cherrypick:"): return "Cherry-pick"
    if name in ("bug","enhancement","documentation","usability","performance",
                "regression","crash","feature proposal","breaks compat","archived"): return "Type"
    return "Other"

lbl["category"] = lbl["label_name"].apply(label_cat)
lbl["display"]  = lbl["label_name"].str.replace(r"^(topic:|platform:)", "", regex=True)

fig6 = px.treemap(lbl, path=["category","display"], values="cnt",
                  color="cnt",
                  color_continuous_scale=[[0,"#1a3a5c"],[0.5,BLUE],[1,PURPLE]])
fig6.update_layout(**base_layout(
    title=dict(text="Godot's Label Universe", font=dict(size=16, color=TEXT), x=0),
    coloraxis_showscale=False,
    height=480,
))
fig6.update_traces(
    textfont=dict(color="white", size=12),
    hovertemplate="<b>%{label}</b><br>%{value:,} PRs<extra></extra>",
)

# ── 7. Open PR Backlog by Age ─────────────────────────────────────────────────
backlog = con.execute("""
    SELECT YEAR(created_at) yr, COUNT(*) open_count
    FROM pull_requests WHERE state='open'
    GROUP BY yr ORDER BY yr
""").df()

fig7 = go.Figure(go.Bar(
    x=backlog["yr"], y=backlog["open_count"],
    marker=dict(color=backlog["yr"],
                colorscale=[[0, RED],[0.5, ORANGE],[1, YELLOW]], showscale=False),
    text=backlog["open_count"],
    textposition="outside",
    textfont=dict(size=11, color=TEXT),
    hovertemplate="<b>%{x}</b><br>%{y} PRs still open<extra></extra>",
    width=0.7,
))
oldest_row = backlog[backlog["yr"] == backlog["yr"].min()]
annotations = []
if not oldest_row.empty:
    annotations = [dict(
        x=int(oldest_row["yr"].values[0]),
        y=int(oldest_row["open_count"].values[0]) + 3,
        text="⚠️ PRs from 2014<br>still open!",
        showarrow=True, arrowhead=2, arrowcolor=RED,
        font=dict(color=RED, size=11), ax=70, ay=-40,
    )]
fig7.update_layout(**base_layout(
    title=dict(text="Open PR Backlog — Submitted Year of Currently Open PRs", font=dict(size=16, color=TEXT), x=0),
    xaxis=dict(dtick=1, gridcolor=BORDER, title="Year submitted"),
    yaxis=dict(gridcolor=BORDER, title="Currently open PRs"),
    showlegend=False,
    annotations=annotations,
))

# ── 8. File Extension Donut ───────────────────────────────────────────────────
ext = con.execute(r"""
    SELECT regexp_extract(filename, '\.(\w+)$', 1) ext, COUNT(*) cnt
    FROM pr_files
    WHERE regexp_extract(filename, '\.(\w+)$', 1) != ''
    GROUP BY ext ORDER BY cnt DESC LIMIT 12
""").df()

ext_palette = [BLUE, CYAN, GREEN, ORANGE, PURPLE, YELLOW, RED,
               "#20B2AA","#DDA0DD","#FA8072","#90EE90", MUTED]
fig8 = go.Figure(go.Pie(
    labels=ext["ext"], values=ext["cnt"],
    marker_colors=ext_palette[:len(ext)],
    hole=0.6,
    textinfo="label+percent", textfont=dict(size=12),
    hovertemplate="<b>.%{label}</b><br>%{value:,} files (%{percent})<extra></extra>",
))
fig8.update_layout(**base_layout(
    title=dict(text="Codebase DNA — Files Changed by Extension", font=dict(size=16, color=TEXT), x=0),
    legend=dict(orientation="v", x=1.0, y=0.5, bgcolor="rgba(0,0,0,0)"),
    height=400,
    annotations=[dict(text="28,257<br>file changes", x=0.5, y=0.5,
                      font_size=14, font_color=TEXT, showarrow=False, xanchor="center")],
))

# ── Assemble HTML ─────────────────────────────────────────────────────────────
def to_div(fig):
    return fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"displayModeBar": False, "responsive": True})

# Pre-computed insight numbers (from DB earlier)
TOTAL_PRS   = 55_870
MERGED      = 42_188
MERGE_RATE  = f"{100*MERGED/TOTAL_PRS:.1f}%"
OPEN_PRS    = 4_947
WEEK_MERGES = 12226 + 13820 + 4169
WEEK_PCT    = f"{100*WEEK_MERGES/MERGED:.1f}%"
LONG_TAIL   = 3320 + 2301 + 545

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Godot Engine · PR Analytics</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    :root{{
      --bg:#0d1117;--card:#161b22;--border:#30363d;
      --blue:#478CBF;--green:#3FB950;--orange:#FF8C42;
      --red:#F85149;--purple:#BC8CFF;--yellow:#E3B341;--cyan:#79C0FF;
      --text:#e6edf3;--muted:#8b949e;
    }}
    body{{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;line-height:1.6}}

    /* ── Header ── */
    .page-header{{
      background:linear-gradient(135deg,#0d1117 0%,#1a2744 50%,#0d1117 100%);
      border-bottom:1px solid var(--border);
      padding:52px 56px 44px;
      position:relative;overflow:hidden;
    }}
    .page-header::before{{
      content:'';position:absolute;top:-60%;left:-15%;
      width:700px;height:700px;
      background:radial-gradient(circle,rgba(71,140,191,.13) 0%,transparent 65%);
      pointer-events:none;
    }}
    .page-header::after{{
      content:'';position:absolute;bottom:-40%;right:-10%;
      width:500px;height:500px;
      background:radial-gradient(circle,rgba(188,140,255,.08) 0%,transparent 65%);
      pointer-events:none;
    }}
    .header-eyebrow{{font-size:11px;font-weight:700;letter-spacing:.18em;
      text-transform:uppercase;color:var(--blue);margin-bottom:10px}}
    .page-header h1{{font-size:42px;font-weight:700;letter-spacing:-.8px;
      margin-bottom:10px;color:var(--text)}}
    .page-header h1 span{{color:var(--blue)}}
    .header-sub{{color:var(--muted);font-size:15px;max-width:580px;line-height:1.7}}

    /* ── KPI Strip ── */
    .kpi-strip{{display:grid;grid-template-columns:repeat(4,1fr);
      gap:1px;background:var(--border);border-bottom:1px solid var(--border)}}
    .kpi{{background:var(--card);padding:30px 36px;position:relative}}
    .kpi::after{{content:'';position:absolute;bottom:0;left:36px;right:36px;
      height:3px;border-radius:3px 3px 0 0}}
    .kpi.blue::after{{background:var(--blue)}}
    .kpi.green::after{{background:var(--green)}}
    .kpi.orange::after{{background:var(--orange)}}
    .kpi.purple::after{{background:var(--purple)}}
    .kpi-label{{font-size:11px;font-weight:700;letter-spacing:.1em;
      text-transform:uppercase;color:var(--muted);margin-bottom:10px}}
    .kpi-value{{font-size:40px;font-weight:700;letter-spacing:-1.5px;
      color:var(--text);line-height:1}}
    .kpi-sub{{font-size:12px;color:var(--muted);margin-top:8px}}

    /* ── Content ── */
    .content{{max-width:1440px;margin:0 auto;padding:0 40px 96px}}
    .section{{margin-top:64px}}
    .section-header{{display:flex;align-items:baseline;gap:14px;margin-bottom:6px}}
    .section-num{{font-size:11px;font-weight:700;letter-spacing:.18em;
      text-transform:uppercase;color:var(--blue);min-width:36px}}
    .section-title{{font-size:23px;font-weight:600;letter-spacing:-.3px;color:var(--text)}}
    .section-desc{{font-size:14px;color:var(--muted);margin:6px 0 20px 50px;
      max-width:700px;line-height:1.75}}

    /* ── Chart Card ── */
    .chart-card{{background:var(--card);border:1px solid var(--border);
      border-radius:12px;padding:8px;overflow:hidden}}

    /* ── Insight Pills ── */
    .insights{{display:flex;gap:14px;margin-bottom:18px;flex-wrap:wrap}}
    .insight{{background:var(--card);border:1px solid var(--border);
      border-left:3px solid var(--blue);border-radius:8px;
      padding:12px 18px;font-size:13px;color:var(--muted);flex:1;min-width:200px}}
    .insight strong{{color:var(--text)}}
    .insight.green{{border-left-color:var(--green)}}
    .insight.orange{{border-left-color:var(--orange)}}
    .insight.red{{border-left-color:var(--red)}}
    .insight.purple{{border-left-color:var(--purple)}}

    /* ── Grid Layouts ── */
    .grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
    .grid-big-small{{display:grid;grid-template-columns:3fr 2fr;gap:20px}}

    footer{{border-top:1px solid var(--border);text-align:center;
      padding:28px;font-size:12px;color:var(--muted)}}

    @media(max-width:1000px){{
      .kpi-strip{{grid-template-columns:repeat(2,1fr)}}
      .grid-2,.grid-big-small{{grid-template-columns:1fr}}
      .page-header{{padding:36px 24px 32px}}
      .content{{padding:0 20px 64px}}
    }}
  </style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────────────────────── -->
<header class="page-header">
  <div class="header-eyebrow">Godot Engine · Open Source · GitHub</div>
  <h1>Pull Request <span>Analytics</span></h1>
  <p class="header-sub">
    12 years of community contributions examined — from the first PR in February 2014
    through May 2026. Every merge, every review, every reaction.
  </p>
</header>

<!-- ── KPI Strip ──────────────────────────────────────────────────────────── -->
<div class="kpi-strip">
  <div class="kpi blue">
    <div class="kpi-label">Total Pull Requests</div>
    <div class="kpi-value">55,870</div>
    <div class="kpi-sub">Across 12 years of development</div>
  </div>
  <div class="kpi green">
    <div class="kpi-label">Merge Rate</div>
    <div class="kpi-value">{MERGE_RATE}</div>
    <div class="kpi-sub">42,188 PRs successfully merged</div>
  </div>
  <div class="kpi orange">
    <div class="kpi-label">Median Time to Merge</div>
    <div class="kpi-value">2 days</div>
    <div class="kpi-sub">Mean is 24.5 days — a heavy long tail</div>
  </div>
  <div class="kpi purple">
    <div class="kpi-label">Open PRs Today</div>
    <div class="kpi-value">{OPEN_PRS:,}</div>
    <div class="kpi-sub">Some submitted as far back as 2014</div>
  </div>
</div>

<!-- ── Content ────────────────────────────────────────────────────────────── -->
<div class="content">

  <section class="section">
    <div class="section-header"><span class="section-num">01</span>
      <span class="section-title">12 Years of Godot</span></div>
    <p class="section-desc">
      From a modest 326 PRs in 2014 to over 7,000 per year by 2024. The 2020–2022 surge
      reflects Godot 4.0 development, while 2022–2023 saw an influx driven by the Unity
      pricing controversy pulling developers toward the open-source alternative.
    </p>
    <div class="chart-card">{to_div(fig1)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">02</span>
      <span class="section-title">The Merge Clock</span></div>
    <p class="section-desc">
      The median PR is reviewed and merged in just 2 days — but the mean jumps to 24.5.
      That gap tells a two-speed story: a fast lane of routine fixes, and a long-tail of
      complex features that take months or years to land.
    </p>
    <div class="insights">
      <div class="insight green">
        <strong>{WEEK_PCT}</strong> of all merges happen within the first week
      </div>
      <div class="insight orange">
        <strong>{LONG_TAIL:,} PRs</strong> took longer than a month to merge
      </div>
      <div class="insight red">
        <strong>545 PRs</strong> waited over a full year before being merged
      </div>
    </div>
    <div class="chart-card">{to_div(fig2)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">03</span>
      <span class="section-title">The Contributor Funnel</span></div>
    <p class="section-desc">
      Recognition matters. First-time contributors achieve a 27% merge rate, while
      established collaborators land 93% of their PRs. Core members consistently push
      through the highest volume, forming the engine's backbone.
    </p>
    <div class="chart-card">{to_div(fig3)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">04</span>
      <span class="section-title">The Review Economy</span></div>
    <p class="section-desc">
      AThousandShips alone accounts for 1,885 reviews — nearly double the next contributor.
      The top 5 reviewers handle 40% of all reviews. More striking: only 6.6% of review
      submissions are formal approvals, revealing a culture of discussion over verdict.
    </p>
    <div class="insights">
      <div class="insight red">
        <strong>Top 5 reviewers</strong> handle 40% of all reviews — a significant bus-factor risk
      </div>
      <div class="insight orange">
        <strong>89.2%</strong> of reviews are COMMENTED — discussion without a formal verdict
      </div>
      <div class="insight green">
        <strong>714 Approvals</strong> vs 9,573 comments — formal sign-off is rare
      </div>
    </div>
    <div class="chart-card">{to_div(fig4)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">05</span>
      <span class="section-title">Community Wishlist</span></div>
    <p class="section-desc">
      These open PRs have the most community reactions — work people are actively waiting
      for. GDScript Traits leads with 468 reactions. The list is a window into Godot's
      most-wanted features that haven't yet made it to main.
    </p>
    <div class="chart-card">{to_div(fig5)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">06</span>
      <span class="section-title">Label Universe</span></div>
    <p class="section-desc">
      Enhancement and bug reports are nearly equal — 27,548 vs 26,961. The editor and
      core subsystems dominate the topic labels, while platform coverage spans Android,
      Windows, macOS, and Linux at comparable volumes.
    </p>
    <div class="chart-card">{to_div(fig6)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">07</span>
      <span class="section-title">The Aging Backlog</span></div>
    <p class="section-desc">
      Of the 4,947 currently open PRs, some date back to 2014. The chart reveals not just
      volume but the vintage of deferred decisions — design questions that have remained
      open for years, waiting for the right moment, reviewer, or consensus.
    </p>
    <div class="chart-card">{to_div(fig7)}</div>
  </section>

  <section class="section">
    <div class="section-header"><span class="section-num">08</span>
      <span class="section-title">Codebase DNA</span></div>
    <p class="section-desc">
      C++ (<code>.cpp</code> + <code>.h</code>) accounts for 73% of all file changes — Godot is
      fundamentally an engine built in C++. The modest GDScript (<code>.gd</code>) and C#
      (<code>.cs</code>) numbers reflect that most contributors work at the engine layer,
      not the scripting layer.
    </p>
    <div class="chart-card">{to_div(fig8)}</div>
  </section>

</div>

<footer>
  Data from the Godot Engine GitHub repository · {TOTAL_PRS:,} pull requests ·
  Generated May 2026 · <em>godot_prs.duckdb</em>
</footer>
</body>
</html>
"""

out = "godot_analytics_dashboard.html"
with open(out, "w") as f:
    f.write(HTML)

print(f"✓  Dashboard written to {out}  ({len(HTML):,} bytes)")
