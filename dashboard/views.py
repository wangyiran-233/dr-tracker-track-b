"""Dashboard pages. Two clinical views (Overview, Patient Deep-Dive) for a provider with
30 seconds; one developer page (Parser & Eval Inspector: edge cases + benchmark, browse all
reports, review queue, evaluation, method) behind the sidebar toggle "Show Parser & Eval Inspector"."""

from __future__ import annotations

import html
import json
import re

import pandas as pd
import streamlit as st

from cinv.flag_layer import RED_FLAG_TYPES
from cinv.models import MAX_PLAUSIBLE_EPISODES

from . import data
from .charts import cycle_figure, trend_figure
from .data import SEVERITY_LABELS
from .style import (AMBER, AMBER_LINE, AMBER_TINT, BORDER, RED, RED_LINE, RED_TINT, SLATE, TEAL, TEAL_DARK,
                    TEAL_LINE, TEAL_TINT, esc, tag as _tag)

PAGES: dict = {}          # filled by app.py: name -> st.Page (for switch_page)

FLAG_LABELS = {
    "red_flag": "Red flag", "intra_report_conflict": "Conflict within report",
    "cross_report_conflict": "Conflict between reports", "severity_mismatch": "Severity mismatch",
    "implausible_value": "Implausible value", "unmapped_medication": "Unrecognised medication",
    "duplicate_report": "Duplicate report", "low_confidence": "Low confidence",
    # deterministic flag layer (cinv/flag_layer.py)
    "parser_disagreement": "Rules vs LLM disagree", "distress": "Distress",
    "other_symptom": "Other symptom (outside schema)", "prompt_injection": "Prompt injection",
}
RED_TYPES = set(RED_FLAG_TYPES)       # red_flag + distress: urgent, shown as red flags
SEVERITY_ICON = {"urgent": "🔴", "warning": "⚠️", "info": "ℹ️"}
HL = {   # evidence highlight tints (low alpha; text stays in ink)
    "nausea": "rgba(20,184,166,.24)", "vomiting": "rgba(220,38,38,.18)", "retching": "rgba(220,38,38,.18)",
    "appetite_loss": "rgba(15,118,110,.14)", "unable_to_keep_fluids": "rgba(220,38,38,.28)",
    "medication": "rgba(71,85,105,.18)", "weight": "rgba(217,119,6,.22)",
}
# Alert card / status tag styles: (label, ink, background, border)
TAGS = {
    "red": ("RED FLAG", RED, RED_TINT, RED_LINE),
    "weight": ("WEIGHT ALERT", AMBER, AMBER_TINT, AMBER_LINE),
    "silent": ("SILENT DROP-OFF", "#344054", "#F2F4F7", "#D0D5DD"),
    "review": ("TO REVIEW", SLATE, "#F1F5F9", "#CBD5E1"),
    "stable": ("STABLE", TEAL, TEAL_TINT, TEAL_LINE),
}
SORT_NOTE = ("Sorted by type (red flag → weight alert → silent drop-off), then within type: "
             "most recent red flag, largest weight loss, longest silence")


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def page_head(title: str, eyebrow: str | None = None) -> bool:
    """Eyebrow label + page title; returns False (with an error) if the database is not ready."""
    if eyebrow:
        st.html(f"<span class='dt-eyebrow'>{esc(eyebrow)}</span>")
    st.title(title)
    msg = data.db_ready()
    if msg:
        st.error(msg)
        return False
    return True


def subtitle(text: str) -> None:
    st.html(f"<div class='dt-sub'>{text}</div><hr class='dt-rule'>")


def tag(kind: str, text: str | None = None) -> str:
    label, ink, bg, line = TAGS[kind]
    return _tag(text or label, ink, bg, line)


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
    labels = {"nausea_0_10": "nausea {}/10", "vomit_episodes_24h": "vomiting {}×",
              "appetite_loss_0_10": "appetite loss {}/10", "kept_fluids_down": "fluids kept down: {}",
              "prn_antiemetic_doses_24h": "PRN doses {}", "weight_kg": "weight {} kg"}
    parts = [labels[k].format(v) if k in labels else f"{k.replace('_', ' ')} {v}"
             for k, v in q.items() if k != "comments" and v is not None]
    return "questionnaire — " + " · ".join(parts[:5])


def _clip(s: str, n: int = 170) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _ago(days: int) -> str:
    return "today" if days == 0 else ("1 day ago" if days == 1 else f"{days} days ago")


def _go(page: str) -> None:
    if page in PAGES:
        st.switch_page(PAGES[page])


def _go_deepdive(pid: str) -> None:
    st.session_state["pid"] = pid
    _go("deepdive")


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


def current_pid() -> str:
    """Patient shown in the Deep-Dive (sidebar badge): the last one opened, else the first."""
    if data.db_ready():
        return ""
    ids = list(data.patients()["patient_id"])
    pid = st.session_state.get("pid")
    return pid if pid in ids else (ids[0] if ids else "")


def attention_count() -> int:
    """Number of patients with at least one Needs Attention card (sidebar badge)."""
    if data.db_ready():
        return 0
    return len({i["pid"] for i in attention_items(data.snapshot())})


def _as_of_text() -> str:
    return f"Data as of {data.as_of():%b %d, %Y}"


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def attention_items(snap: pd.DataFrame) -> list[dict]:
    """One card per alert: open red flags, weight below the alert line, and silent
    drop-offs (no report for >= SILENT_DAYS days before the reference date)."""
    ref = data.as_of()
    pts = data.patients().set_index("patient_id")
    names = {p: short_name(n) for p, n in zip(snap["patient_id"], snap["display_name"])}
    queue = data.review_queue()
    items: list[dict] = []

    for _, f in queue[queue["flag_type"].isin(RED_TYPES)].iterrows():
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
        if quote.startswith("weight_kg="):          # questionnaire field, not free text
            quote = f"questionnaire — weight {w['weight_kg']:.1f} kg"
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
                                f"(reported on {before['days_reported']} of the last 7 days)",
                      "quote": f"Last report {when:%b %d %H:%M}: {words}",
                      "regimen": pts.loc[pid, "regimen_name"], "cycle": cp["short"] if cp else "",
                      "days": silent, "when": when.strftime("%b %d"),
                      "status": f"Still no report as of {ref:%b %d}", "sort": -silent})

    order = {"red": 0, "weight": 1, "silent": 2}
    return sorted(items, key=lambda i: (order[i["kind"]], i["sort"]))


def _attention_card(item: dict, idx: int) -> None:
    _, ink, _, _ = TAGS[item["kind"]]
    with st.container(key=f"attncard-{item['kind']}-{idx}", gap="small"):
        st.html(
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:6px 8px;flex-wrap:wrap'>"
            f"<b style='font-size:1.02rem;white-space:nowrap'>{esc(item['pid'])} · {esc(item['name'])}</b>"
            f"{tag(item['kind'])}</div>"
            f"<div style='color:{ink};font-weight:600;font-size:.9rem;margin:.55em 0 .6em;line-height:1.4'>"
            f"{esc(item['reason'])}</div>"
            f"<div class='dt-quote'>“{esc(_clip(item['quote'], 150))}”</div>")
        with st.container(gap="small"):
            st.html(f"<div class='dt-small' style='line-height:1.45'>{esc(item['cycle'])} · {esc(item['regimen'])}"
                    f"<br>{esc(item['when'])} ({_ago(item['days'])}) · <b style='color:#334155'>"
                    f"{esc(item['status'])}</b></div>")
            with st.container(horizontal=True, horizontal_alignment="right"):
                if st.button("Inspect Patient →", key=f"attn_{idx}_{item['pid']}", type="primary"):
                    _go_deepdive(item["pid"])


def needs_attention(snap: pd.DataFrame) -> None:
    """High-visibility section above the summary table; cards in rows of three, equal height."""
    items = attention_items(snap)
    with st.container(key="card_attention"):
        if not items:
            st.html(f"<b style='color:{TEAL}'>✓ Needs Attention: no open red flags, weight alerts or "
                    f"silent drop-offs.</b>")
            return
        n_pat = len({i["pid"] for i in items})
        st.html(
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap'>"
            f"<div style='display:flex;align-items:center;gap:10px'>"
            f"<span style='width:30px;height:30px;border-radius:50%;background:{RED_TINT};color:{RED};display:flex;"
            f"align-items:center;justify-content:center;font-size:.95rem'>⚠</span>"
            f"<span style='font-size:1.2rem;font-weight:700'>Needs Attention ({n_pat} patient"
            f"{'s' if n_pat != 1 else ''}, {len(items)} alert{'s' if len(items) != 1 else ''})</span></div>"
            f"<div class='dt-small' style='text-align:right'>{SORT_NOTE}</div></div>")
        per_row = 4 if len(items) % 4 == 0 else 3      # 4 cards -> one row of 4, never a lone card
        for start in range(0, len(items), per_row):
            cols = st.columns(per_row, gap="medium")
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
    return "<div style='display:flex;flex-wrap:wrap;gap:5px'>" + ("".join(tags) or tag("stable")) + "</div>"


COHORT_COLS = [2.1, 2.1, 2.5, 1.1, 1.6, 1.4, 2.2]
COHORT_HEADS = ["Patient", "Regimen & cycle", "7-day peak nausea / vomiting", "7-day PRN doses",
                "Weight Δ vs baseline", "Last report", "Status"]


def _cohort_cells(r, ref, pts, last) -> list[str]:
    pid = r["patient_id"]
    ws = data.week_summary(pid)
    when = pd.Timestamp(last.loc[pid, "submitted_at"])
    silent = (ref - when.date()).days
    cp = data.cycle_position(pid, ws["end"])
    w = data.latest_weight(pid)
    if pd.isna(r["weight_pct_change"]) or w is None:
        weight = "<span class='dt-small'>no weight</span>"
    else:
        color = AMBER if r["below_weight_alert"] else "#1E293B"
        weight = (f"<span class='dt-mono' style='color:{color};font-size:.86rem'>{r['weight_pct_change']:+.1f}%"
                  f"</span><br><span class='dt-small'>{w['weight_kg']:.1f} kg</span>")
        if r["below_weight_alert"]:
            weight += f"<br><span class='dt-small' style='color:{AMBER}'><b>below alert line</b></span>"
    prn = (f"<span class='dt-mono'>{ws['prn_total']} dose{'s' if ws['prn_total'] != 1 else ''}</span>"
           + (f"<br><span class='dt-small'>{ws['prn_inferred']} inferred</span>" if ws["prn_inferred"] else "")
           if ws["days_reported"] else "<span class='dt-small'>not reported</span>")
    vom_color = RED if ws["vomiting_peak"] else "#1E293B"
    return [
        f"<div style='display:flex;gap:9px;align-items:center'><span class='dt-chip'>{esc(pid)}</span>"
        f"<div style='line-height:1.3'><b>{esc(short_name(r['display_name']))}</b><br>"
        f"<span class='dt-small'>{esc(pts.loc[pid, 'diagnosis'])}</span></div></div>",
        f"<div style='line-height:1.35'>{esc(pts.loc[pid, 'regimen_name'])}<br>"
        f"<span class='dt-small dt-mono'>{esc(cp['short']) if cp else ''}</span></div>",
        f"<div class='dt-mono' style='font-size:.84rem;line-height:1.5'>Nausea {esc(ws['nausea'])}<br>"
        f"<span style='color:{vom_color}'>Vomiting {esc(ws['vomiting'])}</span></div>"
        f"<span class='dt-small'>reported {ws['days_reported']}/7 days</span>",
        prn, weight,
        f"<div style='line-height:1.35'>{when:%b %d %H:%M}<br><span class='dt-small'>{_ago(silent)}</span></div>",
        _status_tags(r, silent),
    ]


def cohort_table(snap: pd.DataFrame) -> None:
    """Patient summary as a grid of rows; clicking anywhere on a row opens the Deep-Dive."""
    ref = data.as_of()
    pts = data.patients().set_index("patient_id")
    last = data.last_reports().set_index("patient_id")
    ids = sorted(snap["patient_id"])
    with st.container(key="card_cohort", gap="small"):
        st.html(
            f"<div style='display:flex;justify-content:space-between;align-items:flex-start;gap:16px'>"
            f"<div><div style='font-size:1.2rem;font-weight:700'>Active Cohort Summary "
            f"({esc(ids[0])}–{esc(ids[-1])})</div>"
            f"<div class='dt-small' style='font-size:.88rem'>Click any patient row to open symptom trends and the "
            f"patient's own words</div></div>"
            f"<span class='dt-chip' style='font-family:inherit;padding:3px 12px'>{len(ids)} enrolled patients</span>"
            f"</div>")
        with st.container(key="cohorthead"):
            for col, h in zip(st.columns(COHORT_COLS, vertical_alignment="bottom"), COHORT_HEADS):
                col.html(f"<div class='dt-th'>{esc(h)}</div>")
        for _, r in snap.iterrows():
            pid = r["patient_id"]
            with st.container(key=f"cohortrow-{pid}"):
                for col, cell in zip(st.columns(COHORT_COLS, vertical_alignment="center"),
                                     _cohort_cells(r, ref, pts, last)):
                    col.html(cell)
                if st.button(f"Open {pid}", key=f"row_{pid}", help=f"Open {pid} in Patient Deep-Dive"):
                    _go_deepdive(pid)


def page_overview() -> None:
    if not page_head("Outpatient CINV Panel Triage", "Outpatient Oncology Triage Board"):
        return
    snap = data.snapshot()
    ref = data.as_of()
    n_reg = data.patients()["regimen_name"].nunique()
    subtitle(f"{len(snap)} active patients · {n_reg} chemotherapy / chemo-RT regimens · {_as_of_text()}")
    needs_attention(snap)
    cohort_table(snap)
    st.caption(f"7-day columns cover {ref - pd.Timedelta(days=6):%b %d}–{ref:%b %d} (as-of date = last day in the "
               "dataset), the same window for every patient. “reported none” = the patient said 0; "
               "“not reported” = no report about it (unknown, not 0) — same convention as the trend chart. "
               "Vomiting peaks are lower bounds for vague counts. Red flags are prominence rules for review, "
               "not diagnoses.")


# ---------------------------------------------------------------------------
# 2. Patient Deep-Dive (Timeline / Cycle-over-Cycle + review flags)
# ---------------------------------------------------------------------------

def patient_pills() -> str:
    pts = data.patients()
    ids = list(pts["patient_id"])
    names = dict(zip(pts["patient_id"], pts["display_name"].map(short_name)))
    q = data.review_queue()
    red = set(q[q["flag_type"].isin(RED_TYPES)]["patient_id"])
    warn = set(data.snapshot().query("below_weight_alert == 1")["patient_id"])
    if st.session_state.get("pid") not in ids:
        st.session_state["pid"] = ids[0]
    if st.session_state.get("pid_pick") != st.session_state["pid"]:
        st.session_state["pid_pick"] = st.session_state["pid"]

    def changed():
        if st.session_state.get("pid_pick"):
            st.session_state["pid"] = st.session_state["pid_pick"]

    def fmt(i: str) -> str:
        dot = ":red[●]" if i in red else (":orange[●]" if i in warn else ":green[●]")
        return f"{dot} {i} · {names[i].split()[0]}"

    st.html("<div class='dt-label' style='text-align:right'>Select patient</div>")
    with st.container(horizontal=True, horizontal_alignment="right"):
        st.pills("Patient", ids, key="pid_pick", on_change=changed, format_func=fmt,
                 label_visibility="collapsed")
    return st.session_state["pid"]


def _kpi(label: str, value: str, sub: str, color: str = TEAL) -> str:
    return (f"<div class='dt-kpi'><div class='l'>{esc(label)}</div>"
            f"<div class='v' style='color:{color}'>{value}</div><div class='s'>{sub}</div></div>")


def _compact_week(ws: dict) -> str:
    """Short headline for the KPI card, e.g. 'Mild (1/4) · 0 vomits' (full wording goes below it)."""
    m = re.match(r"(\w[\w ]*?) \((\d)/4\)", ws["nausea"])
    if m:
        nausea = f"{m.group(1).capitalize()} ({m.group(2)}/4)"
    elif ws["nausea"].startswith("0/4"):
        nausea = "None (0/4)"
    else:
        nausea = "Nausea n/r" if ws["nausea"] == "not reported" else "Nausea ?"
    vp = ws["vomiting_peak"]
    if ws["vomiting"] == "not reported":
        vomit = "vomiting n/r"
    elif vp is None:
        vomit = "vomiting, count ?"
    else:
        ge = "≥" if "≥" in ws["vomiting"] else ""
        vomit = f"{ge}{vp} vomit{'s' if vp != 1 else ''}" + ("/day peak" if vp else "")
    return f"{nausea} · {vomit}"


def kpi_strip(pid: str, p: pd.Series) -> None:
    end = data.as_of()
    cp = data.cycle_position(pid, end)
    ws = data.week_summary(pid, end)
    w = data.latest_weight(pid)
    cards = []
    if cp:
        cards.append(_kpi("Current cycle & phase", f"Cycle {cp['cycle']} · Day {cp['day']}",
                          f"{esc(cp['phase_text'])}<br>as of {end:%b %d}", TEAL_DARK))
    else:
        cards.append(_kpi("Current cycle & phase", "Before cycle 1", f"as of {end:%b %d}", TEAL_DARK))
    peak = []
    if ws["nausea_peak_day"]:
        peak.append(f"nausea peak {ws['nausea_peak_day']:%b %d}")
    if ws["vomiting_peak_day"]:
        peak.append(f"vomiting peak {ws['vomiting_peak_day']:%b %d}")
    alert = bool(ws["vomiting_peak"]) or "severe" in ws["nausea"]
    cards.append(_kpi("7-day peak nausea & vomiting", esc(_compact_week(ws)),
                      f"Nausea: {esc(ws['nausea'])} · vomiting: {esc(ws['vomiting'])}<br>"
                      f"{ws['start']:%b %d}–{ws['end']:%b %d} · reported {ws['days_reported']}/7 days"
                      + (f"<br>{' · '.join(peak)}" if peak else ""), RED if alert else TEAL))
    cards.append(_kpi("7-day PRN rescue use", f"{ws['prn_total']} dose{'s' if ws['prn_total'] != 1 else ''} taken",
                      f"{ws['prn_stated']} patient-stated (drug named) · {ws['prn_inferred']} inferred from the "
                      f"active PRN order (questionnaire count)"))
    if w is None:
        cards.append(_kpi("Weight vs alert threshold", "no weight", ""))
    else:
        below = bool(w["below_alert_line"])
        gap = w["weight_kg"] - w["alert_line_kg"]
        src = "clinic" if w["source"] == "clinic" else "patient-reported"
        cards.append(_kpi("Weight vs alert threshold", f"{w['pct_change']:+.1f}% ({w['weight_kg']:.1f} kg)",
                          ("<b>at/below</b> " if below else f"{gap:.1f} kg above ")
                          + f"alert line {w['alert_line_kg']:.1f} kg (−{p['weight_alert_pct']:g}%, "
                            f"practice-configurable)<br>{src}, {pd.Timestamp(w['measured_date']):%b %d}",
                          AMBER if below else "#1E293B"))
    st.html(f"<div class='dt-kpis'>{''.join(cards)}</div>")


def _fmt_delta(ch: dict) -> str:
    b, a = ch["before"], ch["after"]

    def side(x):
        n = "—" if x["nausea"] is None else f"{x['nausea']}/4"
        v = "—" if x["vomit"] is None else f"{x['vomit']}"
        return f"{n} nausea, {v} vomits/day"

    return f"Before {side(b)} → After {side(a)}"


def regimen_change_panel(pid: str) -> None:
    for ch in data.regimen_delta(pid):
        b, a = ch["before"], ch["after"]
        with st.container(key=f"card_change_{ch['date']:%Y%m%d}"):
            st.markdown(f"**Regimen change {ch['date']:%b %d}: + {ch['med']} {ch['dose']} {ch['frequency']}** "
                        f"— {ch['reason']}")
            cols = st.columns(3)
            for col, label, key in ((cols[0], "Mean worst nausea (0–4)", "nausea"),
                                    (cols[1], "Vomiting episodes per reported day", "vomit"),
                                    (cols[2], "PRN doses per reported day", "prn")):
                if a[key] is None or b[key] is None:
                    col.metric(label, "—", border=True)
                else:
                    col.metric(f"{label}: before → after", f"{b[key]} → {a[key]}",
                               f"{a[key] - b[key]:+.1f}", delta_color="inverse", border=True)
            st.caption(f"Acute + delayed phase days only (cycle days 1–5): {b['days']} reported days before vs "
                       f"{a['days']} after the change. **Observational data — not evidence that the change caused "
                       "the difference**; cycles also differ in dose, hydration, reporting days and chance.")


def _flag_quote(f) -> str:
    """Verbatim evidence of the rows a flag points at: patient quotes first, then questionnaire fields."""
    ev = data.flag_evidence(int(f["flag_id"]))
    quotes = list(dict.fromkeys(f"“{e['evidence_span']}”" for e in ev
                                if e["extraction_method"] != "questionnaire" and isinstance(e["evidence_span"], str)))
    fields = list(dict.fromkeys(e["evidence_span"] for e in ev
                                if e["extraction_method"] == "questionnaire" and isinstance(e["evidence_span"], str)))
    if not quotes and not fields:          # no linked rows (e.g. rows rejected by validation): patient's own text
        words = f["patient_words"] if isinstance(f["patient_words"], str) and f["patient_words"] else None
        return f"“{_clip(words or _trigger_quote(f['description'], None), 220)}”"
    return "  ·  ".join(quotes) + (f"  (questionnaire: {', '.join(fields)})" if fields else "")


def review_drawer(pid: str) -> None:
    """Nurse Review Flags: one amber card per open flag, with the evidence and a reconcile button."""
    q = data.review_queue()
    q = q[q["patient_id"] == pid]
    n = len(q)
    with st.container(key="card_flags"):
        dot = AMBER if n else TEAL
        st.html(
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:8px 16px;flex-wrap:wrap'>"
            f"<div style='display:flex;align-items:center;gap:10px'>"
            f"<span style='width:10px;height:10px;border-radius:50%;background:{dot}'></span>"
            f"<span style='font-size:1.15rem;font-weight:700'>Nurse Review Flags ({n} Open)</span>"
            + (_tag(f"{n} Open", AMBER, "#FEF3C7", AMBER_LINE) if n else "")
            + "</div><div class='dt-small'>Clinical reconciliation log · Structured extraction vs. patient narrative"
              "</div></div>")
        if not n:
            st.caption("No open review flags for this patient.")
        for _, f in q.iterrows():
            fid = int(f["flag_id"])
            with st.container(key=f"flagcard-{fid}"):
                body, act = st.columns([5, 1], vertical_alignment="center")
                urgent = _tag("RED FLAG", RED, RED_TINT, RED_LINE) if f["flag_type"] in RED_TYPES else ""
                when = str(f["submitted_at"]).replace("T", " ")[:16]
                linked = data.flag_evidence(fid)
                reason = _reason(f["description"]) if linked else f["description"]   # keep rejected text visible
                body.html(
                    f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
                    f"{_tag('ACTION NEEDED', AMBER, '#FEF3C7', AMBER_LINE)}{urgent}"
                    f"<b style='font-size:1rem'>{esc(FLAG_LABELS.get(f['flag_type'], f['flag_type']))}</b>"
                    f"<span class='dt-small'>report {esc(f['report_id'])} · {esc(when)}</span></div>"
                    f"<div style='font-size:.9rem;color:#334155;margin:.5em 0'>{esc(reason)}</div>"
                    f"<span class='dt-quote' style='display:inline-block'>{esc(_flag_quote(f))}</span>")
                with act:
                    if st.button("✓ Mark Reconciled", key=f"recon_{fid}", type="primary", width="stretch"):
                        data.set_flag_status(fid, "resolved", "Marked reconciled in Patient Deep-Dive")
                        st.rerun()


LEGEND = ("How to read: teal bars = worst nausea that day (darker = worse) · red line = vomiting episodes "
          "(right axis; \"2-4v\" = vague count range, \"≥3v\" = at least) · green dot at 0 = patient reported 0 "
          "vomiting · hatched NO LOG = no nausea or vomiting report that day (unknown, not 0); a missing point on "
          "the red line = vomiting not mentioned that day · hollow circle = nausea present, intensity not stated · "
          "grey ring = conflicting or implausible reports to review · dotted line = chemo infusion · dashed teal "
          "line = regimen change · weight: square = clinic, circle = patient-reported. Hover any point for the "
          "patient's own words and any conflict / uncertainty flags.")


def page_deepdive() -> None:
    msg = data.db_ready()
    if msg:
        st.title("Patient Deep-Dive")
        st.error(msg)
        return
    left, right = st.columns([1, 1.25], vertical_alignment="bottom")
    with right:
        pid = patient_pills()
    p = data.patients().set_index("patient_id").loc[pid]
    of = data.open_flags(pid)
    reds = of[of["flag_type"].isin(RED_TYPES)]
    below = bool(data.snapshot().set_index("patient_id").loc[pid, "below_weight_alert"])
    badges = (" :red-badge[RED FLAG · OPEN]" if len(reds) else "") + (" :orange-badge[WEIGHT ALERT]" if below else "")
    with left:
        b, meta = st.columns([1, 4], vertical_alignment="center", gap="small")
        if b.button("← Back", key="dd_back", type="tertiary"):
            _go("overview")
        meta.html(f"<div class='dt-mono dt-small' style='white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"
                  f"{esc(p['regimen_name'])} · {esc(p['diagnosis'])}</div>")
        st.title(f"{pid} · {short_name(p['display_name'])}{badges}")
    subtitle(f"Emetogenic risk of regimen: {esc(p['emetogenic_risk'])} · enrolled {esc(p['enrolled_at'])} · "
             f"{_as_of_text()}")

    for _, f in reds.iterrows():
        st.html(f"<div class='dt-alert'><b>Red flag (open)</b> — {esc(f['description'])} · report "
                f"{esc(f['report_id'])}, {esc(f['submitted_at'].replace('T', ' ')[:16])}</div>")

    kpi_strip(pid, p)

    ev = data.day_evidence(pid)
    changes = data.regimen_delta(pid)
    with st.container(key="card_chart"):
        t, ctl = st.columns([1.6, 1], vertical_alignment="top")
        t.html("<div style='font-size:1.15rem;font-weight:700'>Longitudinal CINV Trajectory &amp; Provenance</div>"
               "<div class='dt-small' style='font-size:.88rem'>Hover or tap any day to see the patient's own words, "
               "PRN doses and conflict flags</div>")
        with ctl:
            with st.container(horizontal=True, horizontal_alignment="right"):
                view = st.segmented_control("View", ["Timeline", "Cycle-over-Cycle"], default="Timeline",
                                            key="dd_view", label_visibility="collapsed")
            if changes:
                st.html("<div style='text-align:right'>" + "".join(
                    f"<span class='dt-pill' title='Acute + delayed phase days; observational'>"
                    f"{esc(_fmt_delta(c))}</span>" for c in changes) + "</div>")
        view = view or "Timeline"
        if view == "Timeline":
            fig = trend_figure(data.daily(pid), data.prn(pid), data.weights(pid), data.chemo(pid),
                               data.med_changes(pid), data.flagged_days(pid), p["enrolled_at"], data.as_of(), ev=ev)
            st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
            st.caption(LEGEND + " PRN doses from questionnaires are attributed to the active PRN order (inferred, "
                       "not stated by the patient); days with several active PRN orders stay unattributed and are "
                       "flagged.")
        else:
            ch = data.chemo(pid)
            gaps = ch["d"].diff().dt.days.dropna()
            cycle_len = int(gaps.min()) if len(gaps) else 21
            st.caption("Each chemotherapy cycle overlaid by day of cycle (day 1 = infusion day). Shaded bands: acute "
                       "(~first 24 h) and delayed (~24–120 h) phases, commonly used reference definitions applied at "
                       "day granularity. Gaps = no report that day (unknown, not 0). Hover for the patient's words.")
            fig = cycle_figure(data.daily(pid), ch, data.med_changes(pid), cycle_len, ev=ev)
            st.plotly_chart(fig, width="stretch", config={"displaylogo": False})

    regimen_change_panel(pid)
    review_drawer(pid)
    with st.expander("Medication orders", icon=":material/medication:"):
        st.dataframe(data.orders(pid), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# 3. Parser & Eval Inspector: report vs parsed result, review queue
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
        "medication link": ev.apply(lambda r: "" if not r["med_relation"] else
                                    f"{r['med_relation']} {r['related_med'] or '(unmapped)'}", axis=1),
        "method": ev["extraction_method"], "confidence": ev["confidence"].round(2), "status": ev["status"],
        "evidence": ev["evidence_span"],
    })


def _words_tab() -> None:
    st.caption("Pick a report: the left side shows exactly what the patient wrote (highlighted = text the "
               "parser used as evidence); the right side shows the structured rows and any review flags.")
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


def _queue_tab() -> None:
    queue = data.review_queue()
    st.caption("All open review items, urgent first. Select a row to load its report in the "
               "“Browse all reports” tab.")
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
                st.markdown(f"{SEVERITY_ICON[f['severity']]} **{FLAG_LABELS.get(f['flag_type'], f['flag_type'])}** · {f['status']}  \n"
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


BLIND2 = "Blind set 2"     # results.md section of the fresh blind test (the only results shown on the dashboard)


def _blind2_section(secs: dict[str, str]) -> str:
    return next((v for k, v in secs.items() if k.startswith(BLIND2)), "")


def blind_headline() -> str | None:
    """'4 / 50' style count for 'wrong & unflagged' on blind set 2 with LLM prompt v2 + the deterministic
    flag layer (last column of the blind set 2 table in eval/results.md)."""
    sec = _blind2_section(_md_sections(data.EVAL_DIR / "results.md"))
    for line in sec.splitlines():
        if line.startswith("| Wrong & unflagged"):
            m = re.search(r"\((\d+)/(\d+)\)", line.strip().strip("|").split("|")[-1])
            if m:
                return f"{m.group(1)} / {m.group(2)}"
    return None


def _method_tab() -> None:
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


def _eval_tab() -> None:
    blind = _blind2_section(_md_sections(data.EVAL_DIR / "results.md"))
    if not blind:
        st.caption("eval/results.md not found — run evaluate.py --offline.")
        return
    st.subheader("Blind set 2 — fresh blind test (50 reports: 35 edge cases, 15 routine)")
    summary, _, rest = blind.partition("### ")
    st.markdown(summary)
    if rest:
        with st.expander("By category, every wrong & unflagged report and the missed red flag"):
            st.markdown("### " + rest)
    st.caption("Prompt v2 and the flag layer were frozen before blind set 2 was written. Development-set results "
               "are in eval/results.md, not shown here.")


# ---------------------------------------------------------------------------
# 5. Inspector: edge cases (real hard reports from the demo data) + benchmark
# ---------------------------------------------------------------------------

# (button label, report ids). Reports are real rows of the synthetic dataset; everything shown for them
# (text, parsed rows, flags, rationale, check badge) is read from the database at render time.
EDGE_CASES = [
    ("Questionnaire vs comment conflict · R0035", ["R0035"]),
    ("Next-day correction · R0007 → R0009", ["R0007", "R0009"]),
    ("Exaggerated count · R0089", ["R0089"]),
    ("Can't keep water down · R0117", ["R0117"]),
]
EVENT_KEYS = ["event_id", "symptom_code", "presence", "severity_level", "severity_nrs", "episode_count_min",
              "episode_count_max", "occurred_date", "part_of_day", "evidence_span", "extraction_method",
              "confidence", "status", "supersedes_event_id"]


def _py(v):
    """numpy / NaN -> plain JSON values."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e6:
        return int(v)
    return v


def edge_record(rid: str) -> dict:
    """The stored parse of one report as it sits in SQLite: symptom rows, medication rows, flags."""
    r = data.reports().set_index("report_id").loc[rid]
    ev = data.query(f"SELECT {', '.join(EVENT_KEYS)} FROM symptom_events WHERE report_id = ? ORDER BY event_id",
                    [rid])
    rec = {"report_id": rid, "patient_id": r["patient_id"], "source": r["source"],
           "parse_status": r["parse_status"],
           "symptom_events": [{k: _py(e[k]) for k in EVENT_KEYS} for _, e in ev.iterrows()]}
    it = data.report_intakes(rid)
    if not it.empty:
        rec["medication_intake"] = [{k: _py(i[k]) for k in ("medication", "medication_source", "intake_status",
                                                            "dose_count", "taken_date", "evidence_span")}
                                    for _, i in it.iterrows()]
    fl = data.report_flags(rid)
    rec["review_flags"] = [{"flag_type": f["flag_type"], "severity": f["severity"], "status": f["status"],
                            "event_ids": [int(e["event_id"]) for e in data.flag_evidence(int(f["flag_id"]))]}
                           for _, f in fl.iterrows()]
    return rec


def guardrail_rationale(rids: list[str]) -> list[str]:
    """One sentence per stored flag / correction, describing the code rule that produced it."""
    out = []
    for rid in rids:
        for _, f in data.report_flags(rid).iterrows():
            t, ev = f["flag_type"], data.flag_evidence(int(f["flag_id"]))
            if t == "implausible_value":
                m = re.search(r"count (\d+)-(\d+)", f["description"])
                lit = f"the literal count ({m.group(1)})" if m else "the literal count"
                out.append(f"The parser copied {lit} as written; the code caps any count above "
                           f"{MAX_PLAUSIBLE_EPISODES} episodes, stores it as “at least 1” (min 1, max NULL) and "
                           f"raises an implausible_value flag instead of charting the number.")
            elif t == "red_flag" and f["description"].startswith("Patient reports not keeping fluids"):
                out.append("A present unable_to_keep_fluids row from free text always raises an urgent red_flag — "
                           "a fixed code rule, not the model's judgement — so it is listed first in Needs Attention while open.")
            elif t == "severity_mismatch":
                out.append("The patient rated nausea mild (≤ 1/4), but the same day has "
                           + ", ".join(sorted({e["symptom_code"] for e in ev if e["symptom_code"] != "nausea"}))
                           + "; the code raises severity_mismatch (mild nausea + fluids not kept down or ≥ 4 vomits) rather than "
                             "trusting the self-rating.")
            elif t == "intra_report_conflict":
                q = [e["evidence_span"] for e in ev if e["extraction_method"] == "questionnaire"]
                txt = [e["evidence_span"] for e in ev if e["extraction_method"] != "questionnaire"]
                what = (f"The form answer ({', '.join(q)}) and the comment (“{'; '.join(txt)}”) disagree"
                        if q and txt else "Two rows of this report disagree")
                out.append(f"{what}. Both rows are kept, an intra_report_conflict flag is raised, and the daily "
                           f"chart takes the larger count and marks the day.")
            else:
                out.append(f"{FLAG_LABELS.get(t, t)}: {_reason(f['description']).rstrip('.')}.")
        for _, s in data.superseded_by(rid).iterrows():
            out.append(f"{s['corrected_in']} was parsed as a correction, so the earlier {s['symptom_code']} row "
                       f"(event {s['event_id']} in {rid}, same day) was set to superseded and the new row links to "
                       f"it via supersedes_event_id; the original stays in the database, the chart uses the "
                       f"correction.")
    return out


def _json_html(obj) -> str:
    """Pretty JSON with light syntax colouring for the dark code panel."""
    src = html.escape(json.dumps(obj, indent=2, ensure_ascii=False), quote=False)

    def color(m):
        tok = m.group(0)
        if tok.startswith('"'):
            return f"<span class='{'k' if tok.rstrip().endswith(':') else 's'}'>{tok}</span>"
        return f"<span class='n'>{tok}</span>"

    return re.sub(r'"(?:[^"\\]|\\.)*"\s*:|"(?:[^"\\]|\\.)*"|\b(?:true|false|null)\b|-?\b\d+(?:\.\d+)?\b',
                  color, src)


def _submission_html(rid: str) -> str:
    r = data.reports().set_index("report_id").loc[rid]
    events = data.report_events(rid)
    spans = [(e["evidence_span"], e["symptom_code"], f"{e['symptom_code']} · {e['presence']}")
             for _, e in events.iterrows() if e["extraction_method"] != "questionnaire"]
    spans += [(i["evidence_span"], "medication", f"medication: {i['medication']}")
              for _, i in data.report_intakes(rid).iterrows()]
    flagged = {e["evidence_span"] for _, f in data.report_flags(rid).iterrows()
               for e in data.flag_evidence(int(f["flag_id"]))}
    if r["source"] == "free_text":
        return f"Free text: “{highlight(r['raw_text'], spans)}”"
    q = json.loads(r["questionnaire_json"])
    items = []
    for k, v in q.items():
        if k == "comments" or v is None:
            continue
        cell = f"[{html.escape(k)}: {html.escape(str(v))}]"
        if f"{k}={v}" in flagged:
            cell = (f"<mark title='flagged questionnaire answer' style='background:{HL['weight']};color:inherit;"
                    f"border-radius:4px;padding:1px 3px'>{cell}</mark>")
        items.append(cell)
    out = "Structured form: " + " ".join(items)
    if q.get("comments"):
        out += f"<br>Free-text comment: “{highlight(q['comments'], spans)}”"
    return out


def _edge_case(rids: list[str]) -> None:
    reps = data.reports().set_index("report_id")
    first = reps.loc[rids[0]]
    cp = data.cycle_position(first["patient_id"], pd.Timestamp(first["submitted_at"]).normalize())
    chip = f"{first['patient_id']}" + (f" · {cp['short']}" if cp else "") + f" · {first['source'].replace('_', ' ')}"
    checks = [data.verify_report(r) for r in rids]
    ok = all(c["ok"] for c in checks)
    left, right = st.columns(2, gap="medium")
    with left:
        with st.container(key="card_raw"):
            blocks = "".join(
                f"<div class='dt-small' style='margin:{'0' if i == 0 else '.8rem'} 0 .3rem'>"
                f"{esc(r)} · submitted {esc(reps.loc[r, 'submitted_at'].replace('T', ' ')[:16])}"
                f"{' (original)' if len(rids) > 1 and i == 0 else (' (next report)' if i else '')}</div>"
                f"<div class='dt-quote' style='font-size:.86rem;line-height:1.75'>{_submission_html(r)}</div>"
                for i, r in enumerate(rids))
            st.html(f"<div style='display:flex;justify-content:space-between;align-items:center;gap:8px'>"
                    f"<span class='dt-th' style='font-size:.8rem'>1. Raw patient submission</span>"
                    f"<span class='dt-chip'>{esc(chip)}</span></div><hr class='dt-rule'>{blocks}")
            reasons = guardrail_rationale(rids)
            if reasons:
                st.html(f"<div class='dt-rationale'><b>Guardrail rationale:</b> {esc(' '.join(reasons))}</div>")
    with right:
        recs = [edge_record(r) for r in rids]
        badge = ("<span class='dt-okbadge'>Schema valid · Evidence verified</span>" if ok else
                 "<span class='dt-okbadge bad'>Check failed: "
                 + esc("; ".join(p for c in checks for p in c["problems"])[:120]) + "</span>")
        st.html(f"<div class='dt-code'><div class='hd'><span>2. Validated structured output</span>{badge}</div>"
                f"<pre>{_json_html(recs[0] if len(recs) == 1 else recs)}</pre></div>")


def _cell(text: str) -> tuple[int, int, str] | None:
    m = re.search(r"(\d+)%\s*\((\d+)/(\d+)\)", text)
    return (int(m.group(2)), int(m.group(3)), m.group(1)) if m else None


def _row(section: str, metric: str) -> tuple | None:
    """(rule, LLM v2, LLM v2 + flag layer) cells of one results.md table row: first value column and
    the last two (evaluate.py always puts the flag-layer column last, right after v2)."""
    for line in section.splitlines():
        if line.startswith(f"| {metric}"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rule, v2, layer = _cell(cells[1]), _cell(cells[-2]), _cell(cells[-1])
            if rule and v2 and layer:
                return rule, v2, layer
    return None


# (metric row prefix in the blind set 2 table, label, dataset, impact template, primary KPI?)
B2 = "Blind set 2 · 50 reports"
BENCH = [
    ("Symptom rows - recall", "Symptom rows found (recall)", B2,
     "A missed row is a symptom the provider never sees: v2 misses {vm} of {d}, rules miss {rm}; "
     "the flag layer adds flags only, so rows are unchanged", False),
    ("Report fully correct (symptom rows)", "Report fully correct (symptom rows)", B2,
     "Reports whose symptom rows are all right without any human review", False),
    ("Red flags - recall", "Red flags caught (recall)",
     "Blind set 2 · reports labelled red_flag (distress counts as red flag)",
     "v2 alone misses {vm} of {d}; with the flag layer {lm} missed", False),
    ("Wrong & unflagged", "Wrong & unflagged (silent error)", B2,
     "Errors that reach the chart with no flag: rules {rn}, v2 {vn}, v2 + flag layer {ln} of {d}", True),
    ("Flag rate (reports", "Flag rate (cost)", B2,
     "Share of reports a nurse has to look at: v2 {vn}, v2 + flag layer {ln} of {d}", False),
    ("False-alarm rate (fully", "False alarms (cost)", "Blind set 2 · fully correct reports",
     "Correct reports flagged anyway: v2 {vn}, v2 + flag layer {ln} of {d}", False),
    ("False-alarm rate - routine", "False alarms on routine reports (cost)",
     "Blind set 2 · fully correct routine reports",
     "Everyday reports flagged by mistake: v2 {vn}, v2 + flag layer {ln} of {d}", False),
]


def benchmark_table() -> None:
    sec = _blind2_section(_md_sections(data.EVAL_DIR / "results.md"))
    rows = []
    for metric, label, dataset, impact, primary in BENCH:
        found = _row(sec, metric)
        if not found:
            continue
        (rn, d, rp), (vn, dv, vp), (ln, dl, lp) = found
        rule = f"{rn} / {d} <span class='pct'>({rp}%)</span>"
        v2 = f"{vn} / {dv} <span class='pct'>({vp}%)</span>"
        lay = f"{ln} / {dl} <span class='pct'>({lp}%)</span>"
        if primary:
            rule, v2 = f"<span class='bad'>{rule}</span>", f"<span class='warn'>{v2}</span>"
        kpi = _tag("PRIMARY SAFETY KPI", TEAL, TEAL_TINT, TEAL_LINE) if primary else ""
        rows.append(f"<tr class='{'kpi' if primary else ''}'><td><b>{esc(label)}</b> {kpi}</td>"
                    f"<td class='ds'>{esc(dataset)}</td><td class='num'>{rule}</td><td class='num'>{v2}</td>"
                    f"<td class='num v2'>{lay}</td>"
                    f"<td>{esc(impact.format(rn=rn, vn=vn, ln=ln, d=dl, rm=d - rn, vm=dv - vn, lm=dl - ln))}</td></tr>")
    with st.container(key="card_bench"):
        if not rows:
            st.caption("eval/results.md not found — run evaluate.py --offline.")
            return
        head = blind_headline()
        st.html(
            f"<div style='display:flex;justify-content:space-between;align-items:flex-start;gap:8px 16px;"
            f"flex-wrap:wrap'><div><div style='font-size:1.15rem;font-weight:700'>Extraction &amp; Safety "
            f"Guardrail Benchmark</div><div class='dt-small' style='font-size:.86rem'>Keyword-rule baseline vs LLM "
            f"prompt v2 (Pydantic schema + verbatim-evidence check) vs v2 + deterministic flag layer · blind set 2 "
            f"(50 new reports: 35 edge cases over 22 failure categories, 15 routine; prompt and flag layer frozen "
            f"before it was written) · raw counts; small samples, no significance claimed</div></div>"
            + (f"<span class='dt-chip'>{head} wrong &amp; unflagged · fresh blind set</span>" if head else "")
            + "</div>"
            f"<div style='overflow-x:auto'><table class='dt-bench'><thead><tr><th>Metric</th><th>Dataset</th>"
            f"<th>Rule baseline</th><th>LLM v2</th><th>v2 + flag layer</th><th>Clinical safety impact</th>"
            f"</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")
        st.caption("Full per-metric results, the per-category breakdown and every error are in the Evaluation tab.")


def _edge_tab() -> None:
    have = set(data.reports()["report_id"])
    labels = [lab for lab, rids in EDGE_CASES if all(r in have for r in rids)]
    if labels:
        st.caption("Real hard reports from the demo data. Left: what the patient submitted (highlighted = evidence "
                   "the parser used). Right: the rows stored in SQLite for it, re-checked against the schema and "
                   "the submission when this page renders.")
        pick = st.pills("Edge case", labels, default=labels[0], key="edge_case",
                        label_visibility="collapsed") or labels[0]
        _edge_case(dict(EDGE_CASES)[pick])
    benchmark_table()


def page_inspector() -> None:
    msg = data.db_ready()
    if msg:
        st.title("Parser & Eval Inspector")
        st.error(msg)
        return
    info = data.parser_info()
    run = f" · parsed {info['run_at'].replace('T', ' ')[:16]}" if info["run_at"] else ""
    with st.container(key="card_dev"):
        left, right = st.columns([3, 1], vertical_alignment="center")
        with left:
            st.html(f"{_tag('DEVELOPER AUDIT MODE', AMBER, '#FEF3C7', AMBER_LINE)}"
                    f"<span class='dt-mono' style='color:{AMBER};font-size:.85rem;margin-left:10px'>"
                    f"Pydantic schema + SQLite provenance</span>")
            st.title("Parser & Eval Inspector")
            st.html(f"<div class='dt-sub' style='margin-top:-.5rem'>Developer &amp; evaluator audit view — hidden "
                    f"from the default clinical workflow.</div>"
                    f"<div class='dt-small' style='margin-top:.4rem'>Structured data on the dashboard was extracted "
                    f"by: <b>{esc(info['label'])}</b>{esc(run)} · {_as_of_text()}</div>")
        with right:
            if st.button("Return to Clinical View", key="insp_back", width="stretch"):
                _go("overview")
            head = blind_headline()
            if head:
                st.html(f"<div style='text-align:center;margin-top:6px'><span class='dt-chip' "
                        f"style='font-size:.78rem'>{head} wrong &amp; unflagged · fresh blind set</span></div>")

    t_edge, t_words, t_queue, t_eval, t_method = st.tabs(["Edge cases", "Browse all reports", "Review queue",
                                                          "Evaluation", "Method"])
    with t_edge:
        _edge_tab()
    with t_words:
        _words_tab()
    with t_queue:
        _queue_tab()
    with t_eval:
        _eval_tab()
    with t_method:
        _method_tab()
