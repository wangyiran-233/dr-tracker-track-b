"""Plotly figures in the dashboard's teal palette. Colour roles: nausea = teal bars (darker =
worse), vomiting = red line (red is kept for vomiting and red flags), PRN doses = light teal,
weight alert line = amber. Days with no nausea or vomiting report are hatched "NO LOG" so a
missing day is never read as 0. Nausea (0-4 scale, left axis) and vomiting episodes (count,
right axis) share the main panel; PRN doses and weight sit below on the same date axis."""

from __future__ import annotations

import html
import textwrap

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .data import SEVERITY_LABELS

TEAL = "#0F766E"
TEAL_DARK = "#134E4A"
NAUSEA_SHADES = {0: "#B7E4DA", 1: "#7DD3C0", 2: "#2BB3A3", 3: "#0F766E", 4: "#134E4A"}
VOMIT = "#DC2626"
ZERO = "#10B981"
PRN = "#5EB8AE"
CYCLE_COLORS = ["#0F766E", "#2DD4BF", "#134E4A"]
CYCLE_DASH = ["solid", "dash", "dot"]
CRITICAL = "#B42318"
AMBER = "#D97706"
REVIEW = "#475569"
INK2 = "#475569"
MUTED = "#94A3B8"
GRID = "#EEF2F4"
HATCH = "#CBD5E1"
DAY_MS = 24 * 3600 * 1000   # explicit bar width: a lone bar on a date axis is otherwise drawn hairline-thin
FONT = "system-ui, -apple-system, Segoe UI, sans-serif"
MONO = "ui-monospace, Consolas, monospace"

SEV_TICKS = dict(tickvals=[0, 1, 2, 3, 4], ticktext=[f"{i} ({SEVERITY_LABELS[i]})" for i in range(5)])


def _sev_text(row) -> str:
    lvl = row["max_severity_level"]
    s = "intensity not stated" if pd.isna(lvl) else SEVERITY_LABELS[int(lvl)]
    if not pd.isna(row.get("max_severity_nrs")):
        s += f" (score {int(row['max_severity_nrs'])}/10)"
    return s


def _quotes(quotes) -> str:
    """Verbatim evidence spans for a tooltip: quoted, escaped, wrapped, at most three."""
    if not isinstance(quotes, list) or not quotes:
        return "(no quote stored)"
    out = ["“" + "<br>".join(textwrap.wrap(html.escape(q), 60)) + "”" for q in quotes[:3]]
    if len(quotes) > 3:
        out.append(f"+{len(quotes) - 3} more")
    return "<br>".join(out)


def _prov(df: pd.DataFrame, ev: pd.DataFrame | None, code: str) -> pd.DataFrame:
    """Add hover provenance columns: `quote` (patient's words) and `notes` (open flags /
    uncertainty) for one symptom, keyed on occurred_date."""
    df = df.copy()
    e = (ev[ev["symptom_code"] == code].set_index("occurred_date")
         if ev is not None and len(ev) else pd.DataFrame(columns=["evidence", "notes"]))
    df["quote"] = [_quotes(e.loc[x, "evidence"]) if x in e.index else "(no quote stored)"
                   for x in df["occurred_date"]]
    df["notes"] = [e.loc[x, "notes"] if x in e.index else "none" for x in df["occurred_date"]]
    return df


PROV = "<br><i>Patient's words:</i><br>%{{customdata[{q}]}}<br><i>Conflict / uncertainty:</i> %{{customdata[{n}]}}"


def _style(fig: go.Figure, height: int) -> None:
    fig.update_layout(
        height=height, margin=dict(l=20, r=20, t=96, b=40), font=dict(family=FONT, size=12.5, color="#334155"),
        hovermode="closest", bargap=0.35, barmode="overlay", plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=12)),
        hoverlabel=dict(font=dict(family=FONT), align="left", bgcolor="white", bordercolor="#CBD5E1"),
    )
    fig.update_xaxes(showgrid=False, linecolor="#CBD5E1", ticks="outside", tickcolor="#CBD5E1")
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor="#CBD5E1", automargin=True,
                     tickfont=dict(family=MONO, size=11))


def _cycle_day(chemo: pd.DataFrame, day: pd.Timestamp) -> str:
    past = chemo[chemo["d"] <= day]
    if past.empty:
        return ""
    last = past.iloc[-1]
    return f"C{int(last['cycle_number'])}D{int((day - last['d']).days) + 1}"


def trend_figure(d: pd.DataFrame, prn: pd.DataFrame, wt: pd.DataFrame, chemo: pd.DataFrame,
                 changes: pd.DataFrame, flags: pd.DataFrame, start, end,
                 ev: pd.DataFrame | None = None) -> go.Figure:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    days = pd.date_range(start, end, freq="D")
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.62, 0.14, 0.24],
                        specs=[[{"secondary_y": True}], [{}], [{}]],
                        subplot_titles=("",
                                        "As-needed antiemetic doses", "Weight (kg)"))
    for a in fig.layout.annotations:
        a.update(x=0, xanchor="left", font=dict(size=13, color=INK2))

    is_red = flags["flag_type"].isin(("red_flag", "distress"))
    review_days = flags[~is_red & (flags["severity"] != "info")]
    red = flags[is_red]
    n_all = d[d["symptom_code"] == "nausea"]
    v_all = d[d["symptom_code"] == "vomiting"]

    # ---------------- NO LOG: no nausea and no vomiting report that day (unknown, not 0)
    logged = set(n_all["occurred_date"]) | set(v_all["occurred_date"])
    missing = [x for x in days if x not in logged]
    if missing:
        fig.add_trace(go.Bar(
            x=missing, y=[4.6] * len(missing), width=DAY_MS * 0.7, name="No report that day (unknown, not 0)",
            marker=dict(color="rgba(255,255,255,0)", line=dict(color=HATCH, width=1),
                        pattern=dict(shape="/", fgcolor=HATCH, size=7, solidity=0.25)),
            text=["NO LOG"] * len(missing), textposition="inside", insidetextanchor="middle", textangle=-90,
            textfont=dict(family=MONO, size=9, color="#94A3B8"),
            hovertemplate="%{x|%b %d}<br><b>No report that day</b> (unknown, not 0)<extra></extra>"), 1, 1)

    # ---------------- nausea bars (teal, darker = worse)
    n = _prov(n_all, ev, "nausea")
    n["label"] = n.apply(_sev_text, axis=1)
    known = n[n["max_severity_level"].notna() & (n["presence"] != "uncertain")]
    lv = known["max_severity_level"].astype(int)
    fig.add_trace(go.Bar(
        x=known["occurred_date"], y=lv.clip(lower=0.08) if len(lv) else lv, width=DAY_MS * 0.62,
        name="Nausea severity (0-4)", marker=dict(color=[NAUSEA_SHADES[i] for i in lv], cornerradius=4),
        customdata=known[["label", "n_reports", "quote", "notes"]],
        hovertemplate="%{x|%b %d}<br><b>Nausea: %{customdata[0]}</b> · %{customdata[1]} report(s)"
                      + PROV.format(q=2, n=3) + "<extra></extra>"), 1, 1)
    unstated = n[(n["presence"] == "present") & n["max_severity_level"].isna()]
    if len(unstated):
        fig.add_trace(go.Scatter(
            x=unstated["occurred_date"], y=[1] * len(unstated), mode="markers", name="Nausea, intensity not stated",
            marker=dict(color=TEAL, size=11, symbol="circle-open", line=dict(width=2)),
            customdata=unstated[["quote", "notes"]],
            hovertemplate="%{x|%b %d}<br><b>Nausea present, intensity not stated</b>" + PROV.format(q=0, n=1)
                          + "<extra></extra>"), 1, 1)
    unsure = n[n["presence"] == "uncertain"]
    if len(unsure):
        fig.add_trace(go.Scatter(
            x=unsure["occurred_date"], y=[0.5] * len(unsure), mode="markers+text", name="Patient unsure",
            text=["?"] * len(unsure), textposition="top center", textfont=dict(color=INK2),
            marker=dict(color=MUTED, size=10, symbol="diamond-open", line=dict(width=2)),
            customdata=unsure[["quote", "notes"]],
            hovertemplate="%{x|%b %d}<br><b>Patient unsure whether it was nausea</b>" + PROV.format(q=0, n=1)
                          + "<extra></extra>"), 1, 1)

    # ---------------- vomiting line (red, right axis)
    v = _prov(v_all, ev, "vomiting")
    v["y"] = v["episodes_min"].where(v["presence"] == "present", 0)
    v.loc[(v["presence"] == "present") & v["y"].isna(), "y"] = 1
    vp = v[v["presence"] == "present"].copy()
    vp["rng"] = [str(int(lo)) if hi == lo else (f"≥{int(lo)}" if pd.isna(hi) else f"{int(lo)}-{int(hi)}")
                 for lo, hi in zip(vp["episodes_min"].fillna(1), vp["episodes_max"])]
    line = v.set_index("occurred_date")["y"].reindex(days)
    fig.add_trace(go.Scatter(x=days, y=line, mode="lines", line=dict(color=VOMIT, width=2.2),
                             hoverinfo="skip", showlegend=False, connectgaps=False), 1, 1, secondary_y=True)
    err = (vp["episodes_max"] - vp["episodes_min"]).fillna(0)
    fig.add_trace(go.Scatter(
        x=vp["occurred_date"], y=vp["y"], mode="markers+text", name="Vomiting episodes",
        text=[f"{r}v" for r in vp["rng"]], textposition="top center",
        textfont=dict(family=MONO, size=11, color=VOMIT),
        marker=dict(color=VOMIT, size=10, line=dict(color="white", width=2)),
        error_y=dict(type="data", array=err, arrayminus=[0] * len(vp), color=VOMIT, thickness=1.5, width=5),
        customdata=vp[["rng", "n_reports", "quote", "notes"]],
        hovertemplate="%{x|%b %d}<br><b>Vomiting: %{customdata[0]} episode(s)</b> · %{customdata[1]} report(s)"
                      + PROV.format(q=2, n=3) + "<extra></extra>"), 1, 1, secondary_y=True)
    zero = v[v["presence"] == "absent"]
    fig.add_trace(go.Scatter(
        x=zero["occurred_date"], y=[0] * len(zero), mode="markers", name="Reported 0 vomiting",
        marker=dict(color=ZERO, size=9, line=dict(color="white", width=1.5)),
        customdata=zero[["quote", "notes"]],
        hovertemplate="%{x|%b %d}<br><b>Reported 0 episodes</b> (patient said none)" + PROV.format(q=0, n=1)
                      + "<extra></extra>"), 1, 1, secondary_y=True)

    # ---------------- needs-review rings and red flags
    if len(review_days):
        first = True
        for code in ("nausea", "vomiting"):
            rd = review_days[review_days["symptom_code"] == code].drop_duplicates("occurred_date")
            if rd.empty:
                continue
            src = (known if code == "nausea" else v).set_index("occurred_date")
            col = "max_severity_level" if code == "nausea" else "y"
            ys = [float(src.loc[dt, col]) if dt in src.index and not pd.isna(src.loc[dt, col]) else
                  (1.0 if code == "nausea" else 0.0) for dt in rd["occurred_date"]]
            fig.add_trace(go.Scatter(
                x=rd["occurred_date"], y=ys, mode="markers", name="Needs review (conflict etc.)",
                legendgroup="review", showlegend=first,
                marker=dict(symbol="circle-open", size=22, color=REVIEW, line=dict(width=2)),
                customdata=rd[["flag_type", "description"]],
                hovertemplate="%{x|%b %d}<br><b>Needs review: %{customdata[0]}</b><br>%{customdata[1]}"
                              "<extra></extra>"), 1, 1, secondary_y=(code == "vomiting"))
            first = False
    if len(red):
        rr = red.drop_duplicates("occurred_date")
        fig.add_trace(go.Scatter(
            x=rr["occurred_date"], y=[4.45] * len(rr), mode="markers+text", name="Red flag (open)",
            text=["RED FLAG"] * len(rr), textposition="middle right",
            textfont=dict(color=CRITICAL, size=10.5, family=MONO),
            marker=dict(symbol="diamond", color=CRITICAL, size=12),
            customdata=rr[["description"]],
            hovertemplate="%{x|%b %d}<br><b>Red flag</b><br>%{customdata[0]}<extra></extra>"), 1, 1)

    # ---------------- PRN
    fig.add_trace(go.Bar(x=prn["taken_date"], y=prn["doses"], name="PRN antiemetic doses", marker_color=PRN,
                         width=DAY_MS * 0.6, marker_cornerradius=3,
                         hovertemplate="%{x|%b %d}<br>%{y} as-needed dose(s)<extra></extra>"), 2, 1)

    # ---------------- weight
    if len(wt):
        fig.add_trace(go.Scatter(
            x=wt["measured_date"], y=wt["weight_kg"], mode="lines+markers", name="Weight", showlegend=False,
            line=dict(color=INK2, width=2),
            marker=dict(size=8, color=INK2, symbol=["square" if s == "clinic" else "circle" for s in wt["source"]]),
            customdata=wt[["source", "pct_change"]],
            hovertemplate="%{x|%b %d}<br>%{y:.1f} kg (%{customdata[0]})<br>%{customdata[1]:+.1f}% vs baseline"
                          "<extra></extra>"), 3, 1)
        alert, base = wt["alert_line_kg"].iloc[0], wt["baseline_weight_kg"].iloc[0]
        fig.add_hline(y=alert, line=dict(color=AMBER, dash="dash", width=1.5), row=3, col=1,
                      annotation_text=f"alert line {alert:.1f} kg", annotation_position="bottom right",
                      annotation_font=dict(color=AMBER, size=11))
        fig.add_hline(y=base, line=dict(color=MUTED, dash="dot", width=1), row=3, col=1,
                      annotation_text=f"baseline {base:.1f} kg", annotation_position="top right",
                      annotation_font=dict(color=MUTED, size=11))
        lo = min(wt["weight_kg"].min(), alert) - 0.8
        hi = max(wt["weight_kg"].max(), base) + 0.8
        fig.update_yaxes(range=[lo, hi], row=3, col=1)

    # ---------------- infusion days and medication changes (all panels)
    for _, c in chemo.iterrows():
        fig.add_vline(x=c["d"], line=dict(color=MUTED, dash="dot", width=1.2), row="all", col=1)
        fig.add_annotation(x=c["d"], y=1.0, xref="x", yref="paper", yanchor="bottom", yshift=4, showarrow=False,
                           text=f"<b>C{c['cycle_number']} infusion</b>", font=dict(size=11, color=INK2))
    for _, m in changes.iterrows():
        fig.add_vline(x=m["start_date"], line=dict(color=TEAL_DARK, dash="dash", width=2), row="all", col=1)
        fig.add_annotation(x=m["start_date"], y=0.985, xref="x", yref="paper", yanchor="top", xanchor="left",
                           xshift=6, showarrow=False,
                           text=f"<b>Regimen change:</b> + {m['generic_name']} {m['dose']}",
                           font=dict(size=11.5, color=TEAL_DARK, family=MONO), bgcolor="#F0FAF8",
                           bordercolor=TEAL, borderwidth=1.2, borderpad=5)

    _style(fig, 860)
    fig.update_layout(legend=dict(y=1.07, x=0, xanchor="left"), margin=dict(t=88))
    fig.update_yaxes(range=[0, 4.8], row=1, col=1, secondary_y=False, title_text="Nausea (0-4)", **SEV_TICKS)
    vmax = max(5.0, float(line.max() if line.notna().any() else 0) + float(err.max() if len(err) else 0) + 1.2)
    fig.update_yaxes(range=[0, vmax], row=1, col=1, secondary_y=True, showgrid=False, dtick=1,
                     title_text="Vomiting episodes", title_font=dict(color=VOMIT), tickfont=dict(color=VOMIT))
    pmax = max(3, prn["doses"].max() + 0.5 if len(prn) else 0)
    fig.update_yaxes(range=[0, pmax], row=2, col=1, dtick=1)
    ticks = list(pd.date_range(start, end, freq="7D"))
    fig.update_xaxes(range=[start - pd.Timedelta(hours=18), end + pd.Timedelta(hours=18)],
                     tickvals=ticks, ticktext=[f"{t:%b %d}<br>{_cycle_day(chemo, t)}" for t in ticks])
    return fig


def cycle_figure(d: pd.DataFrame, chemo: pd.DataFrame, changes: pd.DataFrame, max_day: int,
                 ev: pd.DataFrame | None = None) -> go.Figure:
    """Overlay cycles by day of cycle (day 1 = infusion day)."""
    change_date = changes["start_date"].min() if len(changes) else None
    change_med = changes.iloc[0]["generic_name"] if len(changes) else None
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08, row_heights=[0.6, 0.4],
                        subplot_titles=("Nausea by day of cycle", "Vomiting episodes by day of cycle (lower bound)"))
    for a in fig.layout.annotations:
        a.update(x=0, xanchor="left", font=dict(size=13, color=INK2))

    for x0, x1, label, alpha in ((0.5, 1.5, "acute", 0.16), (1.5, 5.5, "delayed", 0.07)):
        # drawn before the traces exist, so empty subplots must not be skipped
        fig.add_vrect(x0=x0, x1=x1, fillcolor=f"rgba(15,118,110,{alpha})", line_width=0, layer="below",
                      row="all", col=1, exclude_empty_subplots=False)
        fig.add_annotation(x=(x0 + x1) / 2, y=1.0, xref="x", yref="paper", yanchor="bottom", yshift=4,
                           showarrow=False, text=f"<b>{label} phase</b>", font=dict(size=11, color=INK2))

    for i, (_, c) in enumerate(chemo.iterrows()):
        color = CYCLE_COLORS[i % len(CYCLE_COLORS)]
        name = f"Cycle {c['cycle_number']}"
        if change_date is not None:
            name += f" ({'after' if c['d'] >= change_date else 'before'} {change_med})"
        cyc = d[(d["cycle_number"] == c["cycle_number"]) & (d["day_of_cycle"] <= max_day)]
        n = cyc[(cyc["symptom_code"] == "nausea") & cyc["max_severity_level"].notna()
                & (cyc["presence"] != "uncertain")].sort_values("day_of_cycle")
        n = _prov(n, ev, "nausea")
        by_day = n.set_index("day_of_cycle").reindex(range(1, max_day + 1))
        fig.add_trace(go.Scatter(
            x=list(range(1, max_day + 1)), y=by_day["max_severity_level"], mode="lines+markers", name=name,
            legendgroup=name, line=dict(color=color, width=2.2, dash=CYCLE_DASH[i % len(CYCLE_DASH)]),
            marker=dict(size=8, color=color, line=dict(color="white", width=1.5)), connectgaps=False,
            customdata=by_day[["quote", "notes"]].fillna(""),
            hovertemplate=f"{name}<br>Day %{{x}} · <b>nausea level %{{y}}</b>" + PROV.format(q=0, n=1)
                          + "<extra></extra>"), 1, 1)
        if len(n):
            last = n.iloc[-1]
            fig.add_annotation(x=last["day_of_cycle"], y=last["max_severity_level"], text=f"C{c['cycle_number']}",
                               showarrow=False, xshift=14, font=dict(color=INK2, size=11), row=1, col=1)
        vo = _prov(cyc[cyc["symptom_code"] == "vomiting"].sort_values("day_of_cycle"), ev, "vomiting")
        fig.add_trace(go.Bar(
            x=vo["day_of_cycle"], y=vo["episodes_min"], name=name, legendgroup=name, showlegend=False,
            marker_color=color, marker_cornerradius=3, customdata=vo[["quote", "notes"]],
            hovertemplate=f"{name}<br>Day %{{x}} · <b>%{{y}} episode(s) (at least)</b>" + PROV.format(q=0, n=1)
                          + "<extra></extra>"), 2, 1)

    _style(fig, 600)
    fig.update_layout(barmode="group", legend=dict(y=1.1))
    fig.update_yaxes(range=[-0.3, 4.6], row=1, col=1, **SEV_TICKS)
    fig.update_yaxes(rangemode="tozero", dtick=2, row=2, col=1)
    fig.update_xaxes(range=[0.5, max_day + 0.5], dtick=1, title_text="Day of cycle (day 1 = infusion day)",
                     row=2, col=1)
    return fig
