"""Dashboard pages. Two clinical views (Overview, Patient Deep-Dive) for a provider
with 30 seconds; two developer pages (Words vs parsed data, Method & evaluation)
sit behind the sidebar toggle "Show Parser & Eval Inspector"."""

from __future__ import annotations

import html
import json
import re

import pandas as pd
import streamlit as st

from . import data
from .charts import cycle_figure, trend_figure
from .data import SEVERITY_LABELS

PAGES: dict = {}          # filled by app.py: name -> st.Page (for switch_page)

FLAG_LABELS = {
    "red_flag": "Red flag", "intra_report_conflict": "Conflict within report",
    "cross_report_conflict": "Conflict between reports", "severity_mismatch": "Severity mismatch",
    "implausible_value": "Implausible value", "unmapped_medication": "Unrecognised medication",
    "duplicate_report": "Duplicate report", "low_confidence": "Low confidence",
}
SEVERITY_ICON = {"urgent": "🔴", "warning": "⚠️", "info": "ℹ️"}
HL = {   # evidence highlight tints (series colours at low alpha; text stays in ink)
    "nausea": "rgba(42,120,214,.22)", "vomiting": "rgba(235,104,52,.26)", "retching": "rgba(235,104,52,.26)",
    "appetite_loss": "rgba(27,175,122,.26)", "unable_to_keep_fluids": "rgba(208,59,59,.24)",
    "medication": "rgba(98,80,214,.22)", "weight": "rgba(137,135,129,.30)",
}
# Alert card / status tag styles: (label, background, ink)
TAGS = {
    "red": ("RED FLAG", "#FDECEC", "#B42318"),
    "weight": ("WEIGHT ALERT", "#FEF3C7", "#92400E"),
    "silent": ("SILENT DROP-OFF", "#E5E7EB", "#374151"),
    "review": ("TO REVIEW", "#FFF4E5", "#9A3412"),
    "stable": ("STABLE", "#E6F4F1", "#0F766E"),
}
SORT_NOTE = ("Sorted by type (red flag → weight alert → silent drop-off), then within type: "
             "most recent red flag, largest weight loss, longest silence")


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def header(title: str, subtitle: str | None = None) -> bool:
    st.title(title)
    msg = data.db_ready()
    if msg:
        st.error(msg)
        return False
    info = data.parser_info()
    run = f" · parsed {info['run_at'].replace('T', ' ')[:16]}" if info["run_at"] else ""
    st.info(f"**Synthetic data — not for clinical use.**  \n"
            f"Structured data on this dashboard was extracted by: **{info['label']}**{run}", icon="🧪")
    if subtitle:
        st.caption(subtitle)
    return True


def esc(s) -> str:
    return html.escape(str(s))


def tag(kind: str, text: str | None = None) -> str:
    label, bg, ink = TAGS[kind]
    return (f"<span style='background:{bg};color:{ink};border:1px solid {ink}33;border-radius:5px;"
            f"padding:1px 7px;font-size:.72rem;font-weight:700;letter-spacing:.04em;white-space:nowrap;"
            f"font-family:ui-monospace,Consolas,monospace'>{esc(text or label)}</span>")


def short_name(name: str) -> str:
    return name.replace(" (synthetic)", "")


def sev_label(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{SEVERITY_LABELS[int(v)]}"


def _trigger_quote(description: str, words: str | None) -> str:
    """The patient's own words behind a red flag: the quoted evidence span if the
    flag has one, else the report text / comment, else the flag description."""
    m = re.search(r"(?:: |^)(\"(.+)\"|'(.+)')\s*$", description)
    return (m.group(2) or m.group(3)) if m else (words or description)


def _reason(description: str) -> str:
    """Flag description without the trailing quoted evidence."""
    return re.sub(r":\s*(\".+\"|'.+')\s*$", "", description).strip()


def _q_summary(qjson: str | None) -> str:
    """Compact questionnaire answers when a report has no free text."""
    if not qjson:
        return "(no text)"
    q = json.loads(qjson)
    if q.get("comments"):
        return q["comments"]
    parts = [f"{k}={v}" for k, v in q.items() if k != "comments" and v is not None]
    return "Questionnaire: " + " · ".join(parts[:5])


def _clip(s: str, n: int = 170) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _ago(days: int) -> str:
    return "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")


def _go_deepdive(pid: str) -> None:
    st.session_state["pid"] = pid
    if "deepdive" in PAGES:
        st.switch_page(PAGES["deepdive"])


def _flag_actions(flag_id: int, key: str) -> None:
    note = st.text_input("Reviewer note", key=f"note_{key}_{flag_id}",
                         placeholder="optional, e.g. 'called patient, confirmed 3 episodes'")
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("Resolve", key=f"res_{key}_{flag_id}"):
        data.set_flag_status(int(flag_id), "resolved", note)
        st.rerun()
    if b2.button("Dismiss", key=f"dis_{key}_{flag_id}"):
        data.set_flag_status(int(flag_id), "dismissed", note)
        st.rerun()


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def attention_items(snap: pd.DataFrame) -> list[dict]:
    """One card per alert: open red flags, weight below the alert line, and silent
    drop-offs (no report for >= SILENT_DAYS days before the reference date)."""
    ref = data.reference_date()
    pts = data.patients().set_index("patient_id")
    names = {p: short_name(n) for p, n in zip(snap["patient_id"], snap["display_name"])}
    queue = data.review_queue()
    items: list[dict] = []

    for _, f in queue[queue["flag_type"] == "red_flag"].iterrows():
        pid, when = f["patient_id"], pd.Timestamp(f["submitted_at"])
        cp = data.cycle_position(pid, when.normalize())
        items.append({"kind": "red", "pid": pid, "name": names.get(pid, pid),
                      "reason": _reason(f["description"]),
                      "quote": _trigger_quote(f["description"], f["patient_words"]),
                      "regimen": pts.loc[pid, "regimen_name"], "cycle": cp["short"] if cp else "",
                      "days": (ref - when.date()).days, "when": when.strftime("%b %d"),
                      "status": "Still open — not yet reviewed", "sort": -when.timestamp()})

    for _, r in snap.iterrows():
        pid = r["patient_id"]
        if not r["below_weight_alert"]:
            continue
        w = data.latest_weight(pid)
        if w is None:
            continue
        mdate = pd.Timestamp(w["measured_date"])
        cp = data.cycle_position(pid, mdate)
        src = "clinic weight" if w["source"] == "clinic" else "patient-reported"
        quote = w["evidence_span"] if isinstance(w["evidence_span"], str) else f"{w['weight_kg']:.1f} kg"
        items.append({"kind": "weight", "pid": pid, "name": names[pid],
                      "reason": f"{w['pct_change']:+.1f}% vs baseline — at/below the practice alert line "
                                f"({w['alert_line_kg']:.1f} kg = −{pts.loc[pid, 'weight_alert_pct']:g}%)",
                      "quote": f"{quote}  ({src})",
                      "regimen": pts.loc[pid, "regimen_name"], "cycle": cp["short"] if cp else "",
                      "days": (ref - mdate.date()).days, "when": mdate.strftime("%b %d"),
                      "status": "Still below the line on the latest weight", "sort": float(w["pct_change"])})

    last = data.last_reports().set_index("patient_id")
    for _, r in snap.iterrows():
        pid = r["patient_id"]
        lr = last.loc[pid]
        when = pd.Timestamp(lr["submitted_at"])
        silent = (ref - when.date()).days
        if silent < data.SILENT_DAYS:
            continue
        before = data.week_summary(pid)
        cp = data.cycle_position(pid, when.normalize())
        words = lr["words"] if isinstance(lr["words"], str) and lr["words"] else _q_summary(lr["questionnaire_json"])
        items.append({"kind": "silent", "pid": pid, "name": names[pid],
                      "reason": f"{silent} days without any report "
                                f"(reported on {before['days_reported']}/7 days in the week before)",
                      "quote": f"Last report {when:%b %d %H:%M}: {words}",
                      "regimen": pts.loc[pid, "regimen_name"], "cycle": cp["short"] if cp else "",
                      "days": silent, "when": when.strftime("%b %d"),
                      "status": f"Still no report as of {ref:%b %d}", "sort": -silent})

    order = {"red": 0, "weight": 1, "silent": 2}
    return sorted(items, key=lambda i: (order[i["kind"]], i["sort"]))


def _attention_card(item: dict, idx: int) -> None:
    _, bg, ink = TAGS[item["kind"]]
    with st.container(border=True):
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:8px'>"
            f"<b style='font-size:1.02rem'>{esc(item['pid'])} · {esc(item['name'])}</b>{tag(item['kind'])}</div>"
            f"<div style='color:{ink};font-weight:600;margin:.45em 0 .5em'>{esc(item['reason'])}</div>"
            f"<div style='background:{bg};border-radius:8px;padding:8px 10px;font-family:ui-monospace,Consolas,"
            f"monospace;font-size:.85rem;line-height:1.45'>“{esc(_clip(item['quote']))}”</div>"
            f"<div style='font-size:.82rem;opacity:.8;margin-top:.55em'>{esc(item['cycle'])} · "
            f"{esc(item['regimen'])}</div>"
            f"<div style='font-size:.82rem;margin-top:.15em'>{esc(item['when'])} ({_ago(item['days'])}) · "
            f"<b>{esc(item['status'])}</b></div>",
            unsafe_allow_html=True)
        if st.button("Inspect Patient →", key=f"attn_{idx}_{item['pid']}", type="primary"):
            _go_deepdive(item["pid"])


def needs_attention(snap: pd.DataFrame) -> None:
    """High-visibility banner above the summary table."""
    items = attention_items(snap)
    if not items:
        st.success("✓ Needs Attention: no open red flags, weight alerts or silent drop-offs.")
        return
    n_pat = len({i["pid"] for i in items})
    with st.container(border=True):
        left, right = st.columns([2, 3], vertical_alignment="center")
        left.markdown(f"#### ⚠️ Needs Attention ({n_pat} patient{'s' if n_pat != 1 else ''}, "
                      f"{len(items)} alert{'s' if len(items) != 1 else ''})")
        right.markdown(f"<div style='text-align:right;font-size:.85rem;opacity:.8'>{SORT_NOTE}</div>",
                       unsafe_allow_html=True)
        per_row = 3
        for start in range(0, len(items), per_row):
            cols = st.columns(per_row)
            for col, (i, item) in zip(cols, list(enumerate(items))[start:start + per_row]):
                with col:
                    _attention_card(item, i)


def _status_tags(r, silent_days: int) -> str:
    tags = []
    if r["open_red_flags"] > 0:
        tags.append(tag("red"))
    if r["below_weight_alert"]:
        tags.append(tag("weight"))
    if silent_days >= data.SILENT_DAYS:
        tags.append(tag("silent", f"SILENT {silent_days}D"))
    other = int(r["open_flags"]) - int(r["open_red_flags"])
    if other > 0:
        tags.append(tag("review", f"{other} TO REVIEW"))
    return " ".join(tags) or tag("stable")


def cohort_table(snap: pd.DataFrame) -> str:
    """Summary table as HTML so long cells wrap instead of being cut off."""
    ref = data.reference_date()
    pts = data.patients().set_index("patient_id")
    last = data.last_reports().set_index("patient_id")
    th = ("text-align:left;padding:8px 10px;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;"
          "opacity:.7;border-bottom:1px solid rgba(120,120,120,.35);vertical-align:bottom")
    td = "padding:10px;vertical-align:top;border-bottom:1px solid rgba(120,120,120,.18);white-space:normal"
    heads = ["Patient", "Regimen &amp; cycle", "7-day peak nausea / vomiting", "7-day PRN doses",
             "Weight vs baseline", "Last report", "Status"]
    rows = []
    for _, r in snap.iterrows():
        pid = r["patient_id"]
        ws = data.week_summary(pid)
        when = pd.Timestamp(last.loc[pid, "submitted_at"])
        silent = (ref - when.date()).days
        cp = data.cycle_position(pid, ws["end"])
        if pd.isna(r["weight_pct_change"]):
            weight = "no weight"
        else:
            weight = f"{r['weight_pct_change']:+.1f}%"
            if r["below_weight_alert"]:
                weight += "<br><small style='color:#B42318'><b>below alert line</b></small>"
        prn = (f"{ws['prn_total']}" + (f"<br><small>{ws['prn_inferred']} inferred</small>" if ws["prn_inferred"] else "")
               if ws["days_reported"] else "not reported")
        cells = [
            f"<b>{esc(pid)}</b> · {esc(short_name(r['display_name']))}",
            f"{esc(pts.loc[pid, 'regimen_name'])}<br><small style='opacity:.75'>{esc(cp['short']) if cp else ''} "
            f"at last report</small>",
            f"Nausea: {esc(ws['nausea'])}<br>Vomiting: {esc(ws['vomiting'])}"
            f"<br><small style='opacity:.75'>reported {ws['days_reported']}/7 days</small>",
            prn, weight,
            f"{when:%b %d %H:%M}<br><small style='opacity:.75'>{_ago(silent)}</small>",
            _status_tags(r, silent),
        ]
        rows.append("<tr>" + "".join(f"<td style='{td}'>{c}</td>" for c in cells) + "</tr>")
    return (f"<div style='overflow-x:auto'><table style='width:100%;border-collapse:collapse;font-size:.9rem'>"
            f"<thead><tr>{''.join(f'<th style={chr(39)}{th}{chr(39)}>{h}</th>' for h in heads)}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def page_overview() -> None:
    if not header("Patients — pre-visit overview"):
        return
    snap = data.snapshot()
    ref = data.reference_date()
    needs_attention(snap)

    st.subheader(f"Active cohort ({len(snap)} patients)")
    st.markdown(cohort_table(snap), unsafe_allow_html=True)
    st.caption(f"Static synthetic snapshot: “today” = {ref:%b %d, %Y}, the most recent report in the dataset. "
               "7-day columns count back from each patient's latest report. “reported none” = the patient said 0; "
               "“not reported” = no report about it (unknown, not 0) — same convention as the trend chart. "
               "Vomiting peaks are lower bounds for vague counts. Red flags are prominence rules for review, "
               "not diagnoses.")
    names = dict(zip(snap["patient_id"], snap["display_name"].map(short_name)))
    pick = st.pills("Open a patient in Patient Deep-Dive", list(snap["patient_id"]), key="ov_pick",
                    format_func=lambda p: f"{p} · {names[p]}")
    if pick:
        _go_deepdive(pick)


# ---------------------------------------------------------------------------
# 2. Patient Deep-Dive (Timeline / Cycle-over-Cycle + review flags)
# ---------------------------------------------------------------------------

def patient_pills() -> str:
    pts = data.patients()
    ids = list(pts["patient_id"])
    names = dict(zip(pts["patient_id"], pts["display_name"].map(short_name)))
    red = set(data.review_queue().query("flag_type == 'red_flag'")["patient_id"])
    if st.session_state.get("pid") not in ids:
        st.session_state["pid"] = ids[0]
    if st.session_state.get("pid_pick") != st.session_state["pid"]:
        st.session_state["pid_pick"] = st.session_state["pid"]

    def changed():
        if st.session_state.get("pid_pick"):
            st.session_state["pid"] = st.session_state["pid_pick"]

    st.pills("Patient", ids, key="pid_pick", on_change=changed,
             format_func=lambda i: f"{'🔴 ' if i in red else ''}{i} · {names[i]}")
    return st.session_state["pid"]


def _kpi(col, label: str, value: str, sub: str, alert: bool = False) -> None:
    color = "#B42318" if alert else "#0F766E"
    with col.container(border=True, height="stretch"):
        st.markdown(f"<div style='font-size:.85rem;opacity:.75'>{esc(label)}</div>"
                    f"<div style='font-size:1.25rem;font-weight:700;color:{color};margin:.2em 0;line-height:1.3'>"
                    f"{value}</div><div style='font-size:.82rem;opacity:.8'>{sub}</div>",
                    unsafe_allow_html=True)


def kpi_strip(pid: str, p: pd.Series) -> None:
    end = data.as_of(pid)
    cp = data.cycle_position(pid, end)
    ws = data.week_summary(pid, end)
    w = data.latest_weight(pid)
    c = st.columns(4)
    if cp:
        _kpi(c[0], "Current cycle & phase", f"Cycle {cp['cycle']} · Day {cp['day']}",
             f"{esc(cp['phase_text'])}<br>as of last report, {end:%b %d}", alert=cp["phase"] is not None)
    else:
        _kpi(c[0], "Current cycle & phase", "Before cycle 1", f"as of {end:%b %d}")
    peak = []
    if ws["nausea_peak_day"]:
        peak.append(f"nausea peak {ws['nausea_peak_day']:%b %d}")
    if ws["vomiting_peak_day"]:
        peak.append(f"vomiting peak {ws['vomiting_peak_day']:%b %d}")
    _kpi(c[1], "7-day peak nausea & vomiting",
         f"Nausea {esc(ws['nausea'])}<br>Vomiting {esc(ws['vomiting'])}",
         f"{ws['start']:%b %d}–{ws['end']:%b %d} · reported {ws['days_reported']}/7 days"
         + (f"<br>{' · '.join(peak)}" if peak else ""),
         alert=bool(ws["vomiting_peak"]) or "severe" in ws["nausea"])
    _kpi(c[2], "7-day PRN rescue use", f"{ws['prn_total']} dose{'s' if ws['prn_total'] != 1 else ''} taken",
         f"{ws['prn_stated']} patient-stated (drug named) · {ws['prn_inferred']} inferred from the active "
         f"PRN order (questionnaire count)")
    if w is None:
        _kpi(c[3], "Weight vs alert threshold", "no weight", "")
    else:
        below = bool(w["below_alert_line"])
        gap = w["weight_kg"] - w["alert_line_kg"]
        src = "clinic" if w["source"] == "clinic" else "patient-reported"
        _kpi(c[3], "Weight vs alert threshold", f"{w['pct_change']:+.1f}% ({w['weight_kg']:.1f} kg)",
             ("<b>at/below</b> " if below else f"{gap:.1f} kg above ") +
             f"alert line {w['alert_line_kg']:.1f} kg (−{p['weight_alert_pct']:g}%, practice-configurable)"
             f"<br>{src}, {pd.Timestamp(w['measured_date']):%b %d}", alert=below)


def regimen_change_panel(pid: str) -> None:
    for ch in data.regimen_delta(pid):
        b, a = ch["before"], ch["after"]
        with st.container(border=True):
            st.markdown(f"**▍Regimen change {ch['date']:%b %d}: + {ch['med']} {ch['dose']} {ch['frequency']}** "
                        f"— {ch['reason']}")
            cols = st.columns(3)
            for col, label, key, unit in ((cols[0], "Mean worst nausea (0–4)", "nausea", ""),
                                          (cols[1], "Vomiting episodes per reported day", "vomit", ""),
                                          (cols[2], "PRN doses per reported day", "prn", "")):
                if a[key] is None or b[key] is None:
                    col.metric(label, "—", border=True)
                else:
                    col.metric(f"{label}: before → after", f"{b[key]} → {a[key]}",
                               f"{a[key] - b[key]:+.1f}", delta_color="inverse", border=True)
            st.caption(f"Acute + delayed phase days only (cycle days 1–5): {b['days']} reported days before vs "
                       f"{a['days']} after the change. **Observational data — not evidence that the change caused "
                       "the difference**; cycles also differ in dose, hydration, reporting days and chance.")


def review_drawer(pid: str) -> None:
    q = data.review_queue()
    q = q[q["patient_id"] == pid]
    urgent = (q["severity"] == "urgent").any()
    with st.expander(f"🩺 Open review flags ({len(q)}) — nurse reconciliation", expanded=bool(urgent)):
        if q.empty:
            st.caption("No open review flags for this patient.")
        for _, f in q.iterrows():
            with st.container(border=True):
                words = f["patient_words"] if isinstance(f["patient_words"], str) else "(questionnaire, no comment)"
                st.markdown(f"{SEVERITY_ICON.get(f['severity'], '')} **{FLAG_LABELS.get(f['flag_type'], f['flag_type'])}**"
                            f" · report {f['report_id']} · {str(f['submitted_at']).replace('T', ' ')[:16]}  \n"
                            f"{f['description']}  \n*Patient's words:* “{_clip(words, 220)}”")
                _flag_actions(int(f["flag_id"]), "dd")


LEGEND = ("How to read: grey shading = no report about that symptom that day (unknown, not 0) · short orange dash "
          "at 0 = patient reported 0 vomiting episodes · whisker on a bar = vague count range (\"a few times\" = 2–4) "
          "· ▲≥ = at least this many · hollow circle = nausea present but intensity not stated · orange ring = "
          "conflicting or implausible reports to review · dotted line = chemo infusion · violet line = regimen "
          "change · square = clinic weight, circle = patient-reported. Hover any point for the patient's own words "
          "and any conflict / uncertainty flags.")


def page_deepdive() -> None:
    if not header("Patient Deep-Dive"):
        return
    pid = patient_pills()
    p = data.patients().set_index("patient_id").loc[pid]
    st.caption(f"**{pid} · {short_name(p['display_name'])}** · {p['diagnosis']} · {p['regimen_name']} · emetogenic "
               f"risk of regimen: {p['emetogenic_risk']} · enrolled {p['enrolled_at']}")

    for _, f in data.open_flags(pid).query("flag_type == 'red_flag'").iterrows():
        st.error(f"🔴 **Red flag (open)** — {f['description']}  ·  report {f['report_id']}, "
                 f"{f['submitted_at'].replace('T', ' ')[:16]}")

    kpi_strip(pid, p)

    view = st.segmented_control("View", ["Timeline", "Cycle-over-Cycle"], default="Timeline", key="dd_view")
    view = view or "Timeline"
    ev = data.day_evidence(pid)
    regimen_change_panel(pid)
    if view == "Timeline":
        fig = trend_figure(data.daily(pid), data.prn(pid), data.weights(pid), data.chemo(pid),
                           data.med_changes(pid), data.flagged_days(pid), p["enrolled_at"], data.as_of(pid), ev=ev)
        st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
        st.caption(LEGEND + " PRN doses from questionnaires are attributed to the active PRN order (inferred, not "
                   "stated by the patient); days with several active PRN orders stay unattributed and are flagged.")
    else:
        ch = data.chemo(pid)
        gaps = ch["d"].diff().dt.days.dropna()
        cycle_len = int(gaps.min()) if len(gaps) else 21
        st.caption("Each chemotherapy cycle overlaid by day of cycle (day 1 = infusion day). Shaded bands: acute "
                   "(~first 24 h) and delayed (~24–120 h) phases, commonly used reference definitions applied at day "
                   "granularity. Gaps = no report that day (unknown, not 0). Hover for the patient's words.")
        fig = cycle_figure(data.daily(pid), ch, data.med_changes(pid), cycle_len, ev=ev)
        st.plotly_chart(fig, width="stretch", config={"displaylogo": False})

    review_drawer(pid)
    with st.expander("Medication orders"):
        st.dataframe(data.orders(pid), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# 3. Inspector: report vs parsed result + review queue
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return s.lower().replace("’", "'").strip().strip(" .,;:!?\"'")


def highlight(text: str, spans: list[tuple[str, str, str]]) -> str:
    """spans: (evidence text, kind, tooltip). Overlapping spans keep the first."""
    low = text.lower().replace("’", "'")
    marks = []
    for ev, kind, tip in spans:
        if not ev:
            continue
        i = low.find(_norm(ev))
        if i < 0:
            continue
        j = i + len(_norm(ev))
        if any(not (j <= a or i >= b) for a, b, _, _ in marks):
            continue
        marks.append((i, j, kind, tip))
    marks.sort()
    out, pos = [], 0
    for a, b, kind, tip in marks:
        out.append(html.escape(text[pos:a]))
        out.append(f'<mark title="{html.escape(tip)}" style="background:{HL.get(kind, HL["weight"])};'
                   f'color:inherit;border-radius:4px;padding:1px 3px">{html.escape(text[a:b])}</mark>')
        pos = b
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _events_view(ev: pd.DataFrame) -> pd.DataFrame:
    def count(r):
        if pd.isna(r["episode_count_min"]):
            return ""
        lo = int(r["episode_count_min"])
        if pd.isna(r["episode_count_max"]):
            return f"≥ {lo}"
        hi = int(r["episode_count_max"])
        return str(lo) if lo == hi else f"{lo}–{hi}"

    return pd.DataFrame({
        "symptom": ev["symptom_code"], "presence": ev["presence"],
        "severity": ev["severity_level"].map(sev_label),
        "score /10": ev["severity_nrs"].map(lambda x: "" if pd.isna(x) else str(int(x))),
        "count": ev.apply(count, axis=1), "date": ev["occurred_date"], "part of day": ev["part_of_day"],
        "clock time": [("" if not isinstance(t, str) else ("~" if p == "approximate" else "") + t[11:16])
                       for t, p in zip(ev["occurred_start"], ev["time_precision"])],
        "medication link": ev.apply(lambda r: "" if not r["med_relation"] else
                                    f"{r['med_relation']} {r['related_med'] or '(unmapped)'}", axis=1),
        "method": ev["extraction_method"], "confidence": ev["confidence"].round(2), "status": ev["status"],
        "evidence": ev["evidence_span"],
    })


def page_report() -> None:
    if not header("Patient's words vs parsed result",
                  "Pick a report: the left side shows exactly what the patient wrote (highlighted = text the "
                  "parser used as evidence); the right side shows the structured rows and any review flags."):
        return

    queue = data.review_queue()
    pts = ["All patients"] + list(data.patients()["patient_id"])
    c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
    who = c1.pills("Patient", pts, default="All patients", key="rep_pid") or "All patients"
    only_flagged = c2.toggle("Only reports with open review items", value=True)
    reps = data.reports(None if who == "All patients" else who)
    if only_flagged:
        reps = reps[reps["open_flags"] > 0]
    reps = reps.sort_values(["red_flags", "submitted_at"], ascending=[False, False])
    if reps.empty:
        st.success("No reports match this filter.")
    else:
        ids = list(reps["report_id"])
        wanted = st.session_state.get("rid")
        default = ids.index(wanted) if wanted in ids else 0

        def fmt(rid: str) -> str:
            r = reps.set_index("report_id").loc[rid]
            if isinstance(r["raw_text"], str):
                text = r["raw_text"]
            else:
                text = "questionnaire" + (f": {c}" if (c := json.loads(r["questionnaire_json"]).get("comments")) else "")
            badge = "🔴 " if r["red_flags"] else ("⚠️ " if r["open_flags"] else "")
            return f"{badge}{rid} · {r['patient_id']} · {r['submitted_at'][:16].replace('T', ' ')} · {text[:60]}"

        rid = st.selectbox("Report", ids, index=default, format_func=fmt)
        if rid != ids[default]:          # user picked another report; a filter change alone keeps the choice
            st.session_state["rid"] = rid
        if wanted and wanted not in ids:
            st.caption(f"Report {wanted} is hidden by the current filter.")
        _report_detail(reps.set_index("report_id").loc[rid], rid)

    st.divider()
    st.subheader("Review queue")
    st.caption("All open review items, urgent first. Select a row to load its report above.")
    qv = pd.DataFrame({
        "": queue["severity"].map(SEVERITY_ICON),
        "type": queue["flag_type"].map(FLAG_LABELS),
        "patient": queue["patient_id"], "report": queue["report_id"],
        "submitted": queue["submitted_at"].str.replace("T", " ").str[:16],
        "why": queue["description"], "patient's words": queue["patient_words"].fillna("(questionnaire)"),
    })
    ev = st.dataframe(qv, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row")
    rows = ev.selection.rows if ev and ev.selection else []
    if rows:
        target = queue.iloc[rows[0]]["report_id"]
        if target != st.session_state.get("rid"):
            st.session_state["rid"] = target
            st.rerun()


def _report_detail(r: pd.Series, rid: str) -> None:
    events, intakes = data.report_events(rid), data.report_intakes(rid)
    wts, flags = data.report_weights(rid), data.report_flags(rid)
    spans = [(e["evidence_span"], e["symptom_code"], f"{e['symptom_code']} · {e['presence']}")
             for _, e in events.iterrows()]
    spans += [(i["evidence_span"], "medication", f"medication: {i['medication']} ({i['intake_status']}; "
               f"{i['medication_source']})") for _, i in intakes.iterrows()]
    spans += [(w["evidence_span"], "weight", f"weight {w['weight_kg']} kg") for _, w in wts.iterrows()]

    left, right = st.columns([5, 7], gap="large")
    with left:
        st.markdown(f"**{r['patient_id']}** · {r['source'].replace('_', ' ')} · submitted "
                    f"{r['submitted_at'].replace('T', ' ')[:16]}")
        if r["source"] == "free_text":
            body = highlight(r["raw_text"], spans)
        else:
            q = json.loads(r["questionnaire_json"])
            rows = "".join(f"<tr><td style='padding:2px 12px 2px 0;opacity:.75'>{html.escape(k)}</td>"
                           f"<td><b>{html.escape(str(v))}</b></td></tr>"
                           for k, v in q.items() if k != "comments" and v is not None)
            body = f"<table>{rows}</table>"
            if q.get("comments"):
                body += f"<p style='margin-top:.6em'>Comment: “{highlight(q['comments'], spans)}”</p>"
        st.markdown(f"<div style='font-size:1.15rem;line-height:1.8;padding:14px 16px;border-radius:8px;"
                    f"border:1px solid rgba(137,135,129,.35)'>{body}</div>", unsafe_allow_html=True)
        chips = " ".join(f"<span style='background:{HL[k]};border-radius:4px;padding:1px 6px;margin-right:4px;"
                         f"font-size:.8rem'>{k.replace('_', ' ')}</span>"
                         for k in ("nausea", "vomiting", "appetite_loss", "unable_to_keep_fluids", "medication",
                                   "weight"))
        st.markdown(chips, unsafe_allow_html=True)
        sup = data.superseded_by(rid)
        for _, s in sup.iterrows():
            st.caption(f"↻ The {s['symptom_code']} row from this report was corrected later in report "
                       f"{s['corrected_in']} (kept, marked superseded).")

    with right:
        st.markdown("**Extracted symptom rows**")
        if events.empty:
            st.caption("No symptom rows.")
        else:
            st.dataframe(_events_view(events), hide_index=True, width="stretch")
        if not intakes.empty:
            st.markdown("**Medication intake**")
            st.dataframe(intakes, hide_index=True, width="stretch")
            if (intakes["medication_source"] == "inferred from active PRN order").any():
                st.caption("ⓘ The questionnaire asks only how many as-needed doses were taken. The drug shown is "
                           "**inferred** from the single active PRN antiemetic order, not named by the patient.")
        if not wts.empty:
            st.markdown("**Weight**")
            st.dataframe(wts, hide_index=True, width="stretch")
        st.markdown("**Review flags**")
        if flags.empty:
            st.caption("None.")
        for _, f in flags.iterrows():
            with st.container(border=True):
                st.markdown(f"{SEVERITY_ICON[f['severity']]} **{FLAG_LABELS[f['flag_type']]}** · {f['status']}  \n"
                            f"{f['description']}")
                if f["status"] == "open":
                    _flag_actions(int(f["flag_id"]), "rep")
                elif f["resolution_note"]:
                    st.caption(f"Note: {f['resolution_note']}")


# ---------------------------------------------------------------------------
# 4. Inspector: method & evaluation
# ---------------------------------------------------------------------------

def _md_sections(path) -> dict[str, str]:
    if not path.exists():
        return {}
    parts = re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)
    return {p.split("\n", 1)[0].strip(): p.split("\n", 1)[1] if "\n" in p else "" for p in parts[1:]}


def page_method() -> None:
    if not header("Method & evaluation"):
        return
    info = data.parser_info()

    st.subheader("Where the dashboard data comes from")
    st.markdown(f"- Free-text parser for the data shown: **{info['label']}**\n"
                "- Questionnaire answers: deterministic mapping, 0–10 scores stored unchanged. PRN dose counts "
                "are attributed to the single active PRN order and marked *inferred*; with several active PRN "
                "orders the drug stays unknown and is flagged\n"
                "- Raw reports are immutable; every structured row links back to its report and the exact "
                "text it was based on\n"
                "- Conflicts are kept (both versions) and flagged; daily charts take the larger count and mark the "
                "day. Corrections supersede older rows without deleting them")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Extraction runs**")
        st.dataframe(info["runs"], hide_index=True, width="stretch")
    with c2:
        st.markdown("**Rows by extraction method**")
        st.dataframe(data.query("SELECT extraction_method AS method, COUNT(*) AS symptom_rows FROM symptom_events "
                                "GROUP BY 1"), hide_index=True, width="stretch")
        st.dataframe(data.query("SELECT parse_status, COUNT(*) AS reports FROM raw_reports GROUP BY 1"),
                     hide_index=True, width="stretch")

    st.subheader("How a report becomes data")
    st.markdown(
        "1. **LLM reads the language** (Groq, JSON mode): symptoms, presence, counts, severity words, time words, "
        "medication *mentions*, relative day.\n"
        "2. **Code does the rest**: date arithmetic, mapping medication names to the dictionary (no guessed drug "
        "names), counting conventions, and every review flag.\n"
        "3. **Validation**: Pydantic schema (one retry with the error fed back); every row's evidence must appear "
        "verbatim in the report or the row is dropped and flagged; low self-reported confidence is flagged but "
        "treated as a weak signal.\n"
        "4. **Failure handling**: wrong model / auth / quota stops the run; a single failed report falls back to "
        "keyword rules and is flagged.")

    st.subheader("Parser evaluation on the gold set (30 reports)")
    v2 = _md_sections(data.EVAL_DIR / "results_v2.md")
    v1 = _md_sections(data.EVAL_DIR / "results_v1.md")
    ft = next((k for k in v2 if k.startswith("Free-text")), None)
    if ft:
        st.markdown(f"**{ft}** — rule baseline vs LLM prompt v1 vs v2")
        st.markdown(v2[ft])
    else:
        st.caption("eval/results_v2.md not found — run evaluate.py.")
    st.markdown(
        "**v1 → v2 changes** (each targets an observed v1 error): exaggerated counts copied literally so the code "
        "guard can flag them · part of day only from time words, never from submission time · previous report "
        "given as context so corrections can be resolved.  \n"
        "**Read with care:** 23 free-text reports, so one report moves a metric by several points; the keyword "
        "baseline is optimistic because routine reports come from templates and the rules were written after "
        "seeing them. A held-out set in varied wording is the fair test.")
    err_v2 = next((k for k in v2 if k.startswith("Errors - LLM prompt v2")), None)
    if err_v2:
        with st.expander(f"LLM prompt v2 — {err_v2.split('(')[-1].rstrip(')')}", expanded=True):
            st.markdown(v2[err_v2])
    err_v1 = next((k for k in v1 if k.startswith("Errors - Groq")), None)
    if err_v1:
        with st.expander(f"LLM prompt v1 (snapshot) — {err_v1.split('(')[-1].rstrip(')')}"):
            st.markdown(v1[err_v1])
    q = next((k for k in v2 if k.startswith("Questionnaire")), None)
    if q:
        with st.expander("Questionnaire reports (deterministic in all columns)"):
            st.markdown(v2[q])
