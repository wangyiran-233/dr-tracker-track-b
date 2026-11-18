"""Plotly figures. Colour roles (validated reference palette):
nausea = series 1 (blue), vomiting = series 2 (orange), PRN doses = series 3
(aqua); cycles in the overlay use slots 1-3 (all-pairs validated). Status
colours (critical / serious) are reserved for red flags and items needing
review and always come with a text label. No dual axes: each measure gets its
own panel on a shared date axis."""

from __future__ import annotations

import html
import textwrap

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .data import SEVERITY_LABELS

NAUSEA = "#2a78d6"
VOMIT = "#eb6834"
PRN = "#1baf7a"
CYCLE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
CRITICAL = "#d03b3b"
SERIOUS = "#ec835a"
VIOLET = "#6250d6"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
DAY_MS = 24 * 3600 * 1000   # explicit bar width: a lone bar on a date axis is otherwise drawn hairline-thin
NODATA = "rgba(137,135,129,0.16)"
FONT = "system-ui, -apple-system, Segoe UI, sans-serif"

SEV_TICKS = dict(tickvals=[0, 1, 2, 3, 4], ticktext=[SEVERITY_LABELS[i] for i in range(5)])


def _runs(days: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Group sorted days into contiguous runs."""
    out: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for d in sorted(days):
        if out and (d - out[-1][1]).days == 1:
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


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
        height=height, margin=dict(l=90, r=30, t=110, b=50), font=dict(family=FONT, size=13),
        hovermode="closest", bargap=0.35, barmode="overlay",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=12)),
        hoverlabel=dict(font=dict(family=FONT), align="left"),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, ticks="outside", tickcolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID, automargin=True)


def trend_figure(d: pd.DataFrame, prn: pd.DataFrame, wt: pd.DataFrame, chemo: pd.DataFrame,
                 changes: pd.DataFrame, flags: pd.DataFrame, start, end,
                 ev: pd.DataFrame | None = None) -> go.Figure:
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    days = pd.date_range(start, end, freq="D")
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.045,
                        row_heights=[0.31, 0.27, 0.16, 0.26],
                        subplot_titles=("Nausea - worst reported each day", "Vomiting episodes per day",
                                        "As-needed antiemetic doses", "Weight (kg)"))
    for a in fig.layout.annotations:
        a.update(x=0, xanchor="left", font=dict(size=13, color=INK2))

    review_days = flags[(flags["flag_type"] != "red_flag") & (flags["severity"] != "info")]
    red = flags[flags["flag_type"] == "red_flag"]

    # ---------------- nausea
    n = _prov(d[d["symptom_code"] == "nausea"], ev, "nausea")
    n["label"] = n.apply(_sev_text, axis=1)
    known = n[n["max_severity_level"].notna() & (n["presence"] != "uncertain")]
    line = known.set_index("occurred_date")["max_severity_level"].reindex(days)
    fig.add_trace(go.Scatter(x=days, y=line, mode="lines", line=dict(color=NAUSEA, width=2),
                             hoverinfo="skip", showlegend=False, connectgaps=False), 1, 1)
    fig.add_trace(go.Scatter(
        x=known["occurred_date"], y=known["max_severity_level"], mode="markers", name="Nausea",
        marker=dict(color=NAUSEA, size=9, line=dict(color="white", width=2)),
        customdata=known[["label", "n_reports", "quote", "notes"]],
        hovertemplate="%{x|%b %d}<br><b>Nausea: %{customdata[0]}</b> · %{customdata[1]} report(s)"
                      + PROV.format(q=2, n=3) + "<extra></extra>"), 1, 1)
    unstated = n[(n["presence"] == "present") & n["max_severity_level"].isna()]
    if len(unstated):
        fig.add_trace(go.Scatter(
            x=unstated["occurred_date"], y=[1] * len(unstated), mode="markers", name="Nausea, intensity not stated",
            marker=dict(color=NAUSEA, size=10, symbol="circle-open", line=dict(width=2)),
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

    # ---------------- vomiting
    v = _prov(d[d["symptom_code"] == "vomiting"], ev, "vomiting")
    vp = v[v["presence"] == "present"].copy()
    vp["rng"] = [str(int(lo)) if hi == lo else (f"at least {int(lo)}" if pd.isna(hi) else f"{int(lo)}-{int(hi)}")
                 for lo, hi in zip(vp["episodes_min"], vp["episodes_max"])]
    err = (vp["episodes_max"] - vp["episodes_min"]).fillna(0)
    fig.add_trace(go.Bar(
        x=vp["occurred_date"], y=vp["episodes_min"], name="Vomiting episodes", marker_color=VOMIT, width=DAY_MS * 0.6,
        marker_cornerradius=4,
        error_y=dict(type="data", array=err, arrayminus=[0] * len(vp), color=VOMIT, thickness=2, width=6),
        customdata=vp[["rng", "n_reports", "quote", "notes"]],
        hovertemplate="%{x|%b %d}<br><b>Vomiting: %{customdata[0]} episode(s)</b> · %{customdata[1]} report(s)"
                      + PROV.format(q=2, n=3) + "<extra></extra>"), 2, 1)
    open_ended = vp[vp["episodes_max"].isna()]
    if len(open_ended):
        fig.add_trace(go.Scatter(
            x=open_ended["occurred_date"], y=open_ended["episodes_min"] + 0.6, mode="markers+text",
            name="Count open-ended", text=["≥"] * len(open_ended), textposition="top center",
            marker=dict(symbol="triangle-up", color=VOMIT, size=10),
            hovertemplate="%{x|%b %d}<br>At least this many; exact number unknown<extra></extra>"), 2, 1)
    zero = v[v["presence"] == "absent"]
    fig.add_trace(go.Scatter(
        x=zero["occurred_date"], y=[0] * len(zero), mode="markers", name="Reported 0 episodes",
        marker=dict(symbol="line-ew-open", color=VOMIT, size=12, line=dict(width=3)),
        customdata=zero[["quote", "notes"]],
        hovertemplate="%{x|%b %d}<br><b>Reported 0 episodes</b> (patient said none)" + PROV.format(q=0, n=1)
                      + "<extra></extra>"), 2, 1)

    # ---------------- needs-review rings and red flags
    if len(review_days):
        for code, row in (("nausea", 1), ("vomiting", 2)):
            rd = review_days[review_days["symptom_code"] == code].drop_duplicates("occurred_date")
            if rd.empty:
                continue
            src = d[d["symptom_code"] == code].set_index("occurred_date")
            ys = []
            for dt in rd["occurred_date"]:
                r = src.loc[dt] if dt in src.index else None
                if r is None:
                    ys.append(0)
                elif code == "nausea":
                    ys.append(r["max_severity_level"] if not pd.isna(r["max_severity_level"]) else 1)
                else:
                    ys.append(r["episodes_min"] if not pd.isna(r["episodes_min"]) else 0)
            fig.add_trace(go.Scatter(
                x=rd["occurred_date"], y=ys, mode="markers", name="Needs review (conflict etc.)",
                legendgroup="review", showlegend=(row == 1 or "nausea" not in set(review_days["symptom_code"])),
                marker=dict(symbol="circle-open", size=20, color=SERIOUS, line=dict(width=2.5)),
                customdata=rd[["flag_type", "description"]],
                hovertemplate="%{x|%b %d}<br><b>Needs review: %{customdata[0]}</b><br>%{customdata[1]}"
                              "<extra></extra>"), row, 1)
    if len(red):
        rr = red.drop_duplicates("occurred_date")
        fig.add_trace(go.Scatter(
            x=rr["occurred_date"], y=[4.55] * len(rr), mode="markers+text", name="Red flag (open)",
            text=["RED FLAG"] * len(rr), textposition="middle right", textfont=dict(color=CRITICAL, size=11),
            marker=dict(symbol="diamond", color=CRITICAL, size=13),
            customdata=rr[["description"]],
            hovertemplate="%{x|%b %d}<br><b>Red flag</b><br>%{customdata[0]}<extra></extra>"), 1, 1)

    # ---------------- PRN
    fig.add_trace(go.Bar(x=prn["taken_date"], y=prn["doses"], name="PRN antiemetic doses", marker_color=PRN, width=DAY_MS * 0.6,
                         marker_cornerradius=4,
                         hovertemplate="%{x|%b %d}<br>%{y} as-needed dose(s)<extra></extra>"), 3, 1)

    # ---------------- weight
    if len(wt):
        fig.add_trace(go.Scatter(
            x=wt["measured_date"], y=wt["weight_kg"], mode="lines+markers", name="Weight", showlegend=False,
            line=dict(color=INK2, width=2),
            marker=dict(size=9, color=INK2, symbol=["square" if s == "clinic" else "circle" for s in wt["source"]]),
            customdata=wt[["source", "pct_change"]],
            hovertemplate="%{x|%b %d}<br>%{y:.1f} kg (%{customdata[0]})<br>%{customdata[1]:+.1f}% vs baseline"
                          "<extra></extra>"), 4, 1)
        alert, base = wt["alert_line_kg"].iloc[0], wt["baseline_weight_kg"].iloc[0]
        fig.add_hline(y=alert, line=dict(color=CRITICAL, dash="dash", width=1.5), row=4, col=1,
                      annotation_text=f"alert line {alert:.1f} kg", annotation_position="bottom right",
                      annotation_font=dict(color=CRITICAL, size=11))
        fig.add_hline(y=base, line=dict(color=MUTED, dash="dot", width=1), row=4, col=1,
                      annotation_text=f"baseline {base:.1f} kg", annotation_position="top right",
                      annotation_font=dict(color=MUTED, size=11))
        lo = min(wt["weight_kg"].min(), alert) - 0.8
        hi = max(wt["weight_kg"].max(), base) + 0.8
        fig.update_yaxes(range=[lo, hi], row=4, col=1)

    # ---------------- days with no report for that symptom
    for code, row in (("nausea", 1), ("vomiting", 2)):
        have = set(d.loc[d["symptom_code"] == code, "occurred_date"])
        missing = [x for x in days if x not in have]
        for a, b in _runs(missing):
            fig.add_vrect(x0=a - pd.Timedelta(hours=12), x1=b + pd.Timedelta(hours=12), fillcolor=NODATA,
                          line_width=0, layer="below", row=row, col=1)
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="No report that day (unknown, not 0)",
                             marker=dict(symbol="square", size=12, color=NODATA)), 1, 1)

    # ---------------- infusion days and medication changes (all panels)
    for _, c in chemo.iterrows():
        fig.add_vline(x=c["d"], line=dict(color=INK2, dash="dot", width=1.2), row="all", col=1)
        fig.add_annotation(x=c["d"], y=1.0, xref="x", yref="paper", yanchor="bottom", yshift=24, showarrow=False,
                           text=f"<b>Chemo C{c['cycle_number']}</b>", font=dict(size=11, color=INK2))
    for _, m in changes.iterrows():
        fig.add_vline(x=m["start_date"], line=dict(color=VIOLET, width=2.5), row="all", col=1)
        fig.add_annotation(x=m["start_date"], y=0.985, xref="x", yref="paper", yanchor="top", xanchor="left",
                           showarrow=False, text=f"<b>Regimen change:</b> + {m['generic_name']} {m['dose']}",
                           font=dict(size=11, color=VIOLET), bgcolor="rgba(255,255,255,0.85)",
                           bordercolor=VIOLET, borderwidth=1, borderpad=3)

    _style(fig, 900)
    fig.update_layout(legend=dict(y=1.075))
    fig.update_yaxes(range=[-0.3, 4.9], row=1, col=1, **SEV_TICKS)
    vmax = max(4, (vp["episodes_max"].fillna(vp["episodes_min"] + 1).max() if len(vp) else 0) + 1.5)
    fig.update_yaxes(range=[-0.4, vmax], row=2, col=1, dtick=2)
    pmax = max(3, prn["doses"].max() + 0.5 if len(prn) else 0)
    fig.update_yaxes(range=[0, pmax], row=3, col=1, dtick=1)
    fig.update_xaxes(range=[start - pd.Timedelta(hours=18), end + pd.Timedelta(hours=18)],
                     dtick=7 * 24 * 3600 * 1000, tickformat="%b %d", tick0=start)
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

    for x0, x1, label, alpha in ((0.5, 1.5, "acute", 0.22), (1.5, 5.5, "delayed", 0.11)):
        # drawn before the traces exist, so empty subplots must not be skipped
        fig.add_vrect(x0=x0, x1=x1, fillcolor=f"rgba(137,135,129,{alpha})", line_width=0, layer="below",
                      row="all", col=1, exclude_empty_subplots=False)
        fig.add_annotation(x=(x0 + x1) / 2, y=1.0, xref="x", yref="paper", yanchor="bottom", yshift=24,
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
            legendgroup=name, line=dict(color=color, width=2),
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
    fig.update_layout(barmode="group", legend=dict(y=1.12))
    fig.update_yaxes(range=[-0.3, 4.6], row=1, col=1, **SEV_TICKS)
    fig.update_yaxes(rangemode="tozero", dtick=2, row=2, col=1)
    fig.update_xaxes(range=[0.5, max_day + 0.5], dtick=1, title_text="Day of cycle (day 1 = infusion day)",
                     row=2, col=1)
    return fig
