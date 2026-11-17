"""The five dashboard pages. Pages 1-2 are built for a provider with 30
seconds; pages 3-5 explain the data and how it was produced."""

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
    st.info(f"**Synthetic data — for demonstration only, not for clinical use.**  \n"
            f"Structured data on this dashboard was extracted by: **{info['label']}**{run}", icon="🧪")
    if subtitle:
        st.caption(subtitle)
    return True


def patient_picker() -> str:
    pts = data.patients()
    ids = list(pts["patient_id"])
    names = dict(zip(pts["patient_id"], pts["display_name"]))
    pid = st.session_state.get("pid", ids[0])
    pid = st.selectbox("Patient", ids, index=ids.index(pid) if pid in ids else 0,
                       format_func=lambda i: f"{i} · {names[i]}")
    st.session_state["pid"] = pid
    return pid


def sev_label(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{SEVERITY_LABELS[int(v)]}"


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def _trigger_quote(description: str, words: str | None) -> str:
    """The patient's own words behind a red flag: the quoted evidence span if the
    flag has one, else the report text / comment, else the flag description."""
    m = re.search(r"[\"']([^\"']{3,})[\"']\s*$", description)
    return m.group(1) if m else (words or description)


def needs_attention(snap: pd.DataFrame) -> None:
    """Banner above the KPIs: patients with an open red flag or a weight alert, each with
    the triggering words and a one-click jump to that patient's trend page."""
    queue = data.review_queue()
    reds = queue[queue["flag_type"] == "red_flag"]
    items = []
    for _, r in snap.iterrows():
        pid = r["patient_id"]
        quotes = [(_trigger_quote(f["description"], f["patient_words"]), str(f["submitted_at"]).replace("T", " ")[:16])
                  for _, f in reds[reds["patient_id"] == pid].iterrows()]
        weight = (f"Weight {r['weight_pct_change']:+.1f}% vs baseline (below alert line)"
                  if r["below_weight_alert"] and not pd.isna(r["weight_pct_change"]) else None)
        if quotes or weight:
            items.append((pid, r["display_name"].replace(" (synthetic)", ""), quotes, weight))
    if not items:
        st.success("✓ Needs attention: no open red flags or weight alerts.")
        return
    with st.container(border=True):
        st.error(f"### 🚨 Needs Attention — {len(items)} patient{'s' if len(items) != 1 else ''}", icon=None)
        for pid, name, quotes, weight in items:
            left, right = st.columns([5, 1], vertical_alignment="center")
            lines = [f"**{pid} · {name}**"]
            lines += [f"🔴 “{q}” <small>({t})</small>" for q, t in quotes]
            if weight:
                lines.append(f"⚖️ {weight}")
            left.markdown("<br>".join(lines), unsafe_allow_html=True)
            if right.button("Open trend →", key=f"attn_{pid}", width="stretch"):
                st.session_state["pid"] = pid
                st.switch_page(PAGES["trend"])


def page_overview() -> None:
    if not header("Patients — pre-visit overview",
                  "Last 7 days for each patient, counted back from that patient's most recent report. "
                  "Patients needing attention are listed first. Select a row to open the patient."):
        return
    snap = data.snapshot()
    needs_attention(snap)

    cols = st.columns(4)
    for col, (label, value) in zip(cols, [
            ("Patients", len(snap)),
            ("🔴 With open red flag", int((snap["open_red_flags"] > 0).sum())),
            ("⚠️ With open review items", int((snap["open_flags"] > 0).sum())),
            ("⚖️ Weight below alert line", int(snap["below_weight_alert"].fillna(0).astype(bool).sum()))]):
        col.metric(label, value, border=True)

    def attention(r) -> str:
        if r["open_red_flags"] > 0:
            return f"🔴 Red flag ({int(r['open_red_flags'])})"
        if r["open_flags"] > 0:
            return f"⚠️ {int(r['open_flags'])} item(s) to review"
        if r["below_weight_alert"]:
            return "⚖️ Weight alert"
        return "✓ Nothing open"

    def weight(r) -> str:
        if pd.isna(r["weight_pct_change"]):
            return "—"
        s = f"{r['weight_pct_change']:+.1f}%"
        return s + "  ⚠️ below alert line" if r["below_weight_alert"] else s

    view = pd.DataFrame({
        "Patient": snap["patient_id"] + " · " + snap["display_name"].str.replace(" (synthetic)", "", regex=False),
        "Attention": snap.apply(attention, axis=1),
        "Worst nausea": snap["worst_nausea_0_4"].map(sev_label),
        "Vomiting episodes": snap["vomiting_episodes_min"].fillna(0).astype(int).map(
            lambda x: "none reported" if x == 0 else f"≥ {x}"),
        "Days not keeping fluids": snap["days_not_keeping_fluids"].fillna(0).astype(int),
        "PRN doses": snap["prn_doses"].fillna(0).astype(int),
        "Weight vs baseline": snap.apply(weight, axis=1),
        "Reported": snap["days_reported_7d"].map(lambda x: f"{int(x)}/7 days"),
        "Data through": snap["as_of"],
    })
    event = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         column_config={"Attention": st.column_config.TextColumn(width="medium")})
    st.caption("Vomiting counts are lower bounds: vague counts (\"a few times\") contribute their minimum. "
               "Red flags are prominence rules for review, not diagnoses.")
    rows = event.selection.rows if event and event.selection else []
    if rows:
        st.session_state["pid"] = snap.iloc[rows[0]]["patient_id"]
        st.switch_page(PAGES["trend"])


# ---------------------------------------------------------------------------
# 2. Patient trend
# ---------------------------------------------------------------------------

def page_trend() -> None:
    if not header("Patient trend"):
        return
    pid = patient_picker()
    p = data.patients().set_index("patient_id").loc[pid]
    st.caption(f"{p['diagnosis']} · {p['regimen_name']} · emetogenic risk of regimen: {p['emetogenic_risk']} "
               f"(reference classification) · enrolled {p['enrolled_at']}")

    for _, f in data.open_flags(pid).query("flag_type == 'red_flag'").iterrows():
        st.error(f"🔴 **Red flag (open)** — {f['description']}  ·  report {f['report_id']}, "
                 f"{f['submitted_at'].replace('T', ' ')[:16]}")

    s = data.since_last_visit(pid)
    if s:
        st.subheader(f"Since last visit · {s['visit_date']:%b %d} (chemo cycle {s['cycle']}) · {s['n_days']} days")
        cur, prev = s["current"], s["previous"]
        cols = st.columns(5)
        dn = None
        if prev and cur["worst_nausea"] is not None and prev["worst_nausea"] is not None:
            dn = cur["worst_nausea"] - prev["worst_nausea"]
        cols[0].metric("Worst nausea", sev_label(cur["worst_nausea"]),
                       None if dn is None else (f"{dn:+d} level vs last cycle" if dn else "same as last cycle"),
                       delta_color="inverse", border=True)
        vtxt = f"{'≥ ' if cur['vomit_open'] else ''}{cur['vomit_min']}"
        dv = None if not prev else cur["vomit_min"] - prev["vomit_min"]
        cols[1].metric("Vomiting episodes", vtxt, None if dv is None else f"{dv:+d} vs last cycle",
                       delta_color="inverse", border=True)
        dp = None if not prev else cur["prn"] - prev["prn"]
        cols[2].metric("PRN antiemetic doses", cur["prn"], None if dp is None else f"{dp:+d} vs last cycle",
                       delta_color="inverse", border=True)
        if s["weight_now"] is not None:
            dw = None if s["weight_at_visit"] is None else s["weight_now"] - s["weight_at_visit"]
            cols[3].metric("Weight", f"{s['weight_now']:.1f} kg",
                           None if dw is None else f"{dw:+.1f} kg since visit", delta_color="normal", border=True)
            if s["weight_below_alert"]:
                cols[3].markdown(f"⚠️ **below alert line** ({s['weight_pct_baseline']:+.1f}% vs baseline)")
            else:
                cols[3].caption(f"{s['weight_pct_baseline']:+.1f}% vs baseline")
        cols[4].metric("Open review items", s["flags_open_total"],
                       f"{s['flags_since']} new since visit" if s["flags_since"] else "none new",
                       delta_color="off", border=True)
        if s["red_since"]:
            cols[4].markdown(f"🔴 **{s['red_since']} red flag(s) since visit**")
        st.caption(f"Reported on {cur['days_reported']} of {s['n_days']} days. Deltas compare with the same days "
                   f"of the previous cycle. Visits = infusion days in this synthetic dataset.")

    end = data.as_of(pid)
    fig = trend_figure(data.daily(pid), data.prn(pid), data.weights(pid), data.chemo(pid),
                       data.med_changes(pid), data.flagged_days(pid), p["enrolled_at"], end)
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})
    st.caption("How to read: grey shading = no report about that symptom that day (unknown, not zero) · "
               "short orange dash at 0 = patient reported no vomiting · whisker on a bar = vague count range "
               "(\"a few times\" = 2–4) · ▲≥ = at least this many · hollow circle = nausea present but intensity "
               "not stated · orange ring = conflicting or implausible reports to review · dotted line = chemo "
               "infusion · violet line = antiemetic change · square = clinic weight, circle = patient-reported. "
               "PRN doses from questionnaires are attributed to the active PRN order (inferred, not stated by the "
               "patient); days with several active PRN orders stay unattributed and are flagged.")
    with st.expander("Medication orders"):
        st.dataframe(data.orders(pid), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# 3. Cycle comparison
# ---------------------------------------------------------------------------

def page_cycles() -> None:
    if not header("Cycle comparison",
                  "Each chemotherapy cycle overlaid by day of cycle (day 1 = infusion day). Shaded bands: acute "
                  "(~first 24 h) and delayed (~24–120 h) phases, following commonly used reference definitions "
                  "at day granularity. P002 had an antiemetic change between cycles."):
        return
    pid = patient_picker()
    ch = data.chemo(pid)
    gaps = ch["d"].diff().dt.days.dropna()
    cycle_len = int(gaps.min()) if len(gaps) else 21
    fig = cycle_figure(data.daily(pid), ch, data.med_changes(pid), cycle_len)
    st.plotly_chart(fig, width="stretch", config={"displaylogo": False})

    st.subheader("Before vs after the medication change (same cycle phase)")
    ba = data.before_after(pid)
    if ba.empty:
        st.caption("No medication change with a recorded reason for this patient.")
    else:
        ba = ba.rename(columns={"new_med": "medication added", "change_date": "change date",
                                "cinv_phase": "phase", "days_with_data": "days with data",
                                "avg_nausea_level_0_4": "avg nausea level (0–4)",
                                "vomiting_episodes_min": "vomiting episodes (≥)",
                                "prn_antiemetic_doses": "PRN doses"})
        st.dataframe(ba, hide_index=True, width="stretch")
    st.warning("**Observational data, not a causal comparison.** Cycles differ in other ways too (dose, "
               "hydration, reporting days, chance), and each phase has only a few reported days.", icon="⚠️")


# ---------------------------------------------------------------------------
# 4. Report vs parsed result + review queue
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
    c1, c2 = st.columns([1, 1])
    who = c1.selectbox("Patient", pts)
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
                    note = st.text_input("Reviewer note", key=f"note_{f['flag_id']}",
                                         placeholder="optional, e.g. 'called patient, confirmed 3 episodes'")
                    b1, b2, _ = st.columns([1, 1, 3])
                    if b1.button("Resolve", key=f"res_{f['flag_id']}"):
                        data.set_flag_status(int(f["flag_id"]), "resolved", note)
                        st.rerun()
                    if b2.button("Dismiss", key=f"dis_{f['flag_id']}"):
                        data.set_flag_status(int(f["flag_id"]), "dismissed", note)
                        st.rerun()
                elif f["resolution_note"]:
                    st.caption(f"Note: {f['resolution_note']}")


# ---------------------------------------------------------------------------
# 5. Method & evaluation
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
