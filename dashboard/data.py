"""Read-only data access for the dashboard (plus resolving review flags).

Every query goes through `query()`, which is cached and keyed on the DB file's
modification time, so re-parsing or resolving a flag invalidates the cache.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from pydantic import ValidationError

from cinv.common import evidence_in_text
from cinv.models import INFERRED_PRN_TEXT, IntakeEvent, SymptomEvent
from scripts.run_queries import load_queries

ROOT = Path(__file__).resolve().parent.parent
# Default: the committed, pre-built demo database (LLM prompt v2, rebuilt from the cache).
# Override with CINV_DB to point the dashboard at another database, e.g. data/cinv.db.
DB = Path(os.environ.get("CINV_DB") or ROOT / "data" / "cinv_demo.db")
EVAL_DIR = ROOT / "eval"
QUERIES = load_queries(ROOT / "sql" / "queries.sql")

SEVERITY_LABELS = {0: "none", 1: "mild", 2: "moderate", 3: "severe", 4: "very severe"}


@st.cache_data(show_spinner=False)
def _query(sql: str, params: tuple, mtime: float) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def query(sql: str, params: tuple | list = ()) -> pd.DataFrame:
    return _query(sql, tuple(params), DB.stat().st_mtime)


def db_ready() -> str | None:
    """Return an error message if the DB is missing or not parsed yet."""
    if not DB.exists():
        return "Database not found. Run `scripts/init_db.py` and `scripts/parse_reports.py --text-parser llm --offline` first, or check CINV_DB."
    n = query("SELECT COUNT(*) AS n FROM symptom_events")["n"].iloc[0]
    if n == 0:
        return "No parsed events yet. Run `scripts/parse_reports.py --text-parser rule` (or `llm`)."
    return None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def parser_info() -> dict:
    runs = query("SELECT method, model_name, prompt_version, run_at FROM extraction_runs")
    llm = runs[runs["method"] == "llm"]
    if len(llm):
        r = llm.iloc[0]
        label = f"Groq LLM ({r['model_name']}, prompt {r['prompt_version']}); keyword rules as fallback"
    else:
        label = "Keyword-rule baseline (no LLM)"
    run_at = runs["run_at"].max() if len(runs) else None
    return {"label": label, "run_at": run_at, "runs": runs}


# ---------------------------------------------------------------------------
# Patients / overview
# ---------------------------------------------------------------------------

def patients() -> pd.DataFrame:
    return query("SELECT * FROM patients ORDER BY patient_id")


def snapshot() -> pd.DataFrame:
    return query(QUERIES["pre_visit_snapshot"])


def as_of() -> date:
    """The single 'as of' date for every view: the last day in this static synthetic dataset
    (date of the most recent report). 'Days ago' and 'last 7 days' are all counted from it."""
    return date.fromisoformat(query("SELECT MAX(date(submitted_at)) AS d FROM raw_reports")["d"].iloc[0])


# ---------------------------------------------------------------------------
# Per-patient time series
# ---------------------------------------------------------------------------

def daily(patient_id: str) -> pd.DataFrame:
    df = query("SELECT * FROM v_daily_symptom_summary WHERE patient_id = ? ORDER BY occurred_date", [patient_id])
    df["occurred_date"] = pd.to_datetime(df["occurred_date"])
    return df


def prn(patient_id: str) -> pd.DataFrame:
    df = query("""SELECT taken_date, SUM(doses_taken) AS doses
                  FROM v_daily_medication_intake
                  WHERE patient_id = ? AND is_prn_on_date = 1 AND is_antiemetic = 1
                  GROUP BY taken_date ORDER BY taken_date""", [patient_id])
    df["taken_date"] = pd.to_datetime(df["taken_date"])
    return df


def weights(patient_id: str) -> pd.DataFrame:
    df = query("SELECT * FROM v_weight_trend WHERE patient_id = ? ORDER BY measured_date", [patient_id])
    df["measured_date"] = pd.to_datetime(df["measured_date"])
    return df


def chemo(patient_id: str) -> pd.DataFrame:
    df = query("""SELECT cycle_number, date(administered_at) AS d, agents
                  FROM chemo_administrations WHERE patient_id = ? ORDER BY cycle_number""", [patient_id])
    df["d"] = pd.to_datetime(df["d"])
    return df


def med_changes(patient_id: str) -> pd.DataFrame:
    df = query("""SELECT m.generic_name, o.dose, o.frequency, o.start_date, o.change_reason
                  FROM medication_orders o JOIN medications m USING (med_id)
                  WHERE o.patient_id = ? AND o.change_reason IS NOT NULL
                  ORDER BY o.start_date""", [patient_id])
    df["start_date"] = pd.to_datetime(df["start_date"])
    return df


def orders(patient_id: str) -> pd.DataFrame:
    return query("""SELECT m.generic_name AS medication, o.dose, o.route, o.frequency,
                           CASE o.is_prn WHEN 1 THEN 'as needed' ELSE 'scheduled' END AS type,
                           o.start_date, COALESCE(o.end_date, 'active') AS end_date, o.change_reason
                    FROM medication_orders o JOIN medications m USING (med_id)
                    WHERE o.patient_id = ? ORDER BY o.start_date""", [patient_id])


def flagged_days(patient_id: str) -> pd.DataFrame:
    """Open flags located on a symptom day (for chart markers)."""
    df = query("""SELECT DISTINCT f.flag_id, f.flag_type, f.severity, f.description, f.report_id,
                         e.occurred_date, e.symptom_code
                  FROM review_flags f
                  JOIN flag_events fe ON fe.flag_id = f.flag_id AND fe.event_table = 'symptom'
                  JOIN symptom_events e ON e.event_id = fe.event_id
                  WHERE f.patient_id = ? AND f.status = 'open'""", [patient_id])
    df["occurred_date"] = pd.to_datetime(df["occurred_date"])
    return df


def open_flags(patient_id: str) -> pd.DataFrame:
    return query("""SELECT f.flag_id, f.flag_type, f.severity, f.description, f.report_id, r.submitted_at
                    FROM review_flags f JOIN raw_reports r USING (report_id)
                    WHERE f.patient_id = ? AND f.status = 'open'
                    ORDER BY CASE f.severity WHEN 'urgent' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                             r.submitted_at DESC""", [patient_id])


def before_after(patient_id: str) -> pd.DataFrame:
    df = query(QUERIES["symptoms_before_after_med_change"])
    return df[df["patient_id"] == patient_id].drop(columns=["patient_id"])


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def reports(patient_id: str | None = None) -> pd.DataFrame:
    sql = """SELECT r.report_id, r.patient_id, r.submitted_at, r.source, r.raw_text, r.questionnaire_json,
                    r.parse_status,
                    (SELECT COUNT(*) FROM review_flags f WHERE f.report_id = r.report_id AND f.status = 'open') AS open_flags,
                    (SELECT COUNT(*) FROM review_flags f WHERE f.report_id = r.report_id AND f.status = 'open'
                       AND f.flag_type IN ('red_flag', 'distress')) AS red_flags
             FROM raw_reports r"""
    params: list = []
    if patient_id:
        sql += " WHERE r.patient_id = ?"
        params.append(patient_id)
    return query(sql + " ORDER BY r.submitted_at DESC", params)


def report_events(report_id: str) -> pd.DataFrame:
    return query("""SELECT e.event_id, e.symptom_code, e.presence, e.severity_level, e.severity_nrs,
                           e.episode_count_min, e.episode_count_max, e.occurred_date, e.part_of_day,
                           m.generic_name AS related_med, e.med_relation, e.evidence_span,
                           e.extraction_method, e.confidence, e.status, e.supersedes_event_id
                    FROM symptom_events e LEFT JOIN medications m ON m.med_id = e.related_med_id
                    WHERE e.report_id = ? ORDER BY e.event_id""", [report_id])


def report_intakes(report_id: str) -> pd.DataFrame:
    """medication_source separates a drug the patient named from one inferred from
    the orders (questionnaire PRN counts), so an inference never looks like a quote."""
    return query("""SELECT COALESCE(m.generic_name, '(unmapped)') AS medication,
                           CASE WHEN i.med_text_raw = ? THEN 'inferred from active PRN order'
                                WHEN i.extraction_method = 'questionnaire' THEN 'not determined (questionnaire)'
                                ELSE 'patient''s words' END AS medication_source,
                           CASE WHEN i.extraction_method = 'questionnaire' THEN ''
                                ELSE i.med_text_raw END AS patient_wording,
                           i.intake_status, i.dose_count, i.taken_date, i.part_of_day, i.evidence_span, i.confidence
                    FROM medication_intake_events i LEFT JOIN medications m ON m.med_id = i.med_id
                    WHERE i.report_id = ? ORDER BY i.intake_id""", [INFERRED_PRN_TEXT, report_id])


def report_weights(report_id: str) -> pd.DataFrame:
    return query("SELECT measured_date, weight_kg, evidence_span FROM weight_measurements WHERE report_id = ?",
                 [report_id])


def report_flags(report_id: str) -> pd.DataFrame:
    return query("""SELECT flag_id, flag_type, severity, description, status, resolution_note
                    FROM review_flags WHERE report_id = ?
                    ORDER BY CASE severity WHEN 'urgent' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END""", [report_id])


def superseded_by(report_id: str) -> pd.DataFrame:
    """Events in this report that a later correction replaced."""
    return query("""SELECT old.event_id, old.symptom_code, new.report_id AS corrected_in
                    FROM symptom_events old JOIN symptom_events new ON new.supersedes_event_id = old.event_id
                    WHERE old.report_id = ?""", [report_id])


def review_queue() -> pd.DataFrame:
    return query(QUERIES["review_queue"])


def set_flag_status(flag_id: int, status: str, note: str) -> None:
    con = sqlite3.connect(DB)
    with con:
        con.execute("UPDATE review_flags SET status = ?, resolution_note = ?, resolved_at = ? WHERE flag_id = ?",
                    (status, note or None, datetime.now().isoformat(timespec="seconds"), flag_id))
    con.close()


# ---------------------------------------------------------------------------
# Clinical-view helpers (Overview cards, Deep-Dive header, chart provenance)
# ---------------------------------------------------------------------------

SILENT_DAYS = 3          # >= this many days without any report before the as-of date -> "silent drop-off"
PHASES = {"acute": "Acute phase (day 1, ~0-24 h)", "delayed": "Delayed phase (days 2-5, ~24-120 h)"}


def last_reports() -> pd.DataFrame:
    """Latest report per patient with the patient's words (free text or questionnaire comment)."""
    return query("""SELECT r.patient_id, r.report_id, r.submitted_at, r.source,
                           COALESCE(r.raw_text, json_extract(r.questionnaire_json, '$.comments')) AS words,
                           r.questionnaire_json
                    FROM raw_reports r
                    WHERE r.submitted_at = (SELECT MAX(x.submitted_at) FROM raw_reports x
                                            WHERE x.patient_id = r.patient_id)""")


def latest_weight(patient_id: str) -> pd.Series | None:
    df = query("""SELECT t.*, w.evidence_span FROM v_weight_trend t
                  JOIN weight_measurements w ON w.patient_id = t.patient_id AND w.measured_date = t.measured_date
                       AND w.weight_kg = t.weight_kg AND w.status = 'active'
                  WHERE t.patient_id = ? ORDER BY t.measured_date DESC LIMIT 1""", [patient_id])
    return None if df.empty else df.iloc[0]


def cycle_position(patient_id: str, on) -> dict | None:
    """Chemo cycle and day of cycle on a date (day 1 = infusion day) plus the CINV phase."""
    ch = chemo(patient_id)
    on = pd.Timestamp(on)
    past = ch[ch["d"] <= on]
    if past.empty:
        return None
    last = past.iloc[-1]
    day = int((on - last["d"]).days) + 1
    phase = "acute" if day == 1 else ("delayed" if day <= 5 else None)
    return {"cycle": int(last["cycle_number"]), "day": day, "phase": phase,
            "phase_text": PHASES.get(phase, "Outside the acute/delayed window (day 6+)"),
            "short": f"C{int(last['cycle_number'])} D{day}"}


def _fmt_level(v) -> str:
    return f"{SEVERITY_LABELS[int(v)]} ({int(v)}/4)"


def week_summary(patient_id: str, end=None) -> dict:
    """7 days ending at `end` (default: the dataset's as-of date). Keeps 'reported 0'
    (the patient said none) apart from 'not reported' (no report about it at all)."""
    end = pd.Timestamp(end or as_of())
    start = end - pd.Timedelta(days=6)
    d = daily(patient_id)
    w = d[(d["occurred_date"] >= start) & (d["occurred_date"] <= end)]
    nau, vom = w[w["symptom_code"] == "nausea"], w[w["symptom_code"] == "vomiting"]

    n_peak_day = None
    if nau.empty:
        nausea = "not reported"
    elif nau["max_severity_level"].dropna().empty:
        nausea = "present, intensity not stated" if (nau["presence"] == "present").any() else "patient unsure"
    else:
        lvl = int(nau["max_severity_level"].max())
        nausea = _fmt_level(lvl) if lvl else "0/4 (reported none)"
        if lvl:
            n_peak_day = nau.loc[nau["max_severity_level"].idxmax(), "occurred_date"].date()

    v_peak_day = v_peak = None
    if vom.empty:
        vomiting = "not reported"
    else:
        present = vom[vom["presence"] == "present"]
        if present.empty:
            vomiting, v_peak = "0 episodes (reported none)", 0
        elif present["episodes_min"].dropna().empty:
            vomiting = "present, count not stated"
        else:
            i = present["episodes_min"].idxmax()
            v_peak = int(present.loc[i, "episodes_min"])
            hi = present.loc[i, "episodes_max"]
            ge = "≥" if pd.isna(hi) or hi > v_peak else ""
            vomiting = f"peak {ge}{v_peak} episode{'s' if v_peak != 1 else ''}/day"
            v_peak_day = present.loc[i, "occurred_date"].date()

    return {"start": start.date(), "end": end.date(), "nausea": nausea, "nausea_peak_day": n_peak_day,
            "vomiting": vomiting, "vomiting_peak_day": v_peak_day, "vomiting_peak": v_peak,
            "days_reported": int(w["occurred_date"].nunique()), **prn_split(patient_id, start, end)}


def prn_split(patient_id: str, start, end) -> dict:
    """As-needed antiemetic doses taken, split into doses of a drug the patient named vs
    doses attributed to the single active PRN order (questionnaire counts: inferred)."""
    df = query("""SELECT i.med_text_raw, i.dose_count FROM medication_intake_events i
                  JOIN medications m ON m.med_id = i.med_id
                  WHERE i.patient_id = ? AND i.status = 'active' AND i.intake_status = 'taken'
                    AND m.is_antiemetic = 1 AND i.taken_date BETWEEN ? AND ?
                    AND EXISTS (SELECT 1 FROM medication_orders o WHERE o.patient_id = i.patient_id
                                AND o.med_id = i.med_id AND o.is_prn = 1 AND o.start_date <= i.taken_date
                                AND (o.end_date IS NULL OR o.end_date >= i.taken_date))""",
               [patient_id, str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date())])
    inferred = int(df.loc[df["med_text_raw"] == INFERRED_PRN_TEXT, "dose_count"].sum())
    total = int(df["dose_count"].sum())
    return {"prn_total": total, "prn_stated": total - inferred, "prn_inferred": inferred}


def day_evidence(patient_id: str) -> pd.DataFrame:
    """Per day and symptom: the verbatim evidence quotes behind the plotted value and any
    open review flags / uncertainty on those rows (shown in chart tooltips)."""
    ev = query("""SELECT e.event_id, e.occurred_date, e.symptom_code, e.presence, e.evidence_span
                  FROM symptom_events e WHERE e.patient_id = ? AND e.status = 'active'""", [patient_id])
    fl = query("""SELECT fe.event_id, f.flag_type FROM flag_events fe
                  JOIN review_flags f ON f.flag_id = fe.flag_id
                  WHERE fe.event_table = 'symptom' AND f.status = 'open' AND f.patient_id = ?""", [patient_id])
    flags = fl.groupby("event_id")["flag_type"].apply(set).to_dict() if len(fl) else {}
    rows = []
    for (day, code), g in ev.groupby(["occurred_date", "symptom_code"]):
        quotes = list(dict.fromkeys(s for s in g["evidence_span"] if isinstance(s, str) and s))
        tags = set().union(*[flags.get(i, set()) for i in g["event_id"]])
        notes = sorted(t.replace("_", " ") for t in tags)
        if (g["presence"] == "uncertain").any():
            notes.append("patient unsure")
        if g["presence"].nunique() > 1:
            notes.append("reports disagree on presence")
        rows.append({"occurred_date": pd.Timestamp(day), "symptom_code": code, "evidence": quotes,
                     "notes": ", ".join(notes) or "none"})
    return pd.DataFrame(rows, columns=["occurred_date", "symptom_code", "evidence", "notes"])


def regimen_delta(patient_id: str) -> list[dict]:
    """For each medication change: acute + delayed phase days before vs after it (cycle
    phases only, so quiet recovery days don't dilute either side). Observational only."""
    ch, d, p = med_changes(patient_id), daily(patient_id), prn(patient_id)
    ph = d[d["cinv_phase"].isin(["acute", "delayed"])]
    out = []
    for _, c in ch.iterrows():
        res = {"med": c["generic_name"], "dose": c["dose"], "frequency": c["frequency"],
               "date": c["start_date"].date(), "reason": c["change_reason"]}
        for side, part in (("before", ph[ph["occurred_date"] < c["start_date"]]),
                           ("after", ph[ph["occurred_date"] >= c["start_date"]])):
            nau = part[(part["symptom_code"] == "nausea") & part["max_severity_level"].notna()]
            vom = part[part["symptom_code"] == "vomiting"]
            days = list(part["occurred_date"].unique())
            doses = p[p["taken_date"].isin(days)]["doses"].sum()
            res[side] = {"days": len(days),
                         "nausea": None if nau.empty else round(float(nau["max_severity_level"].mean()), 1),
                         "vomit": None if vom.empty else round(float(vom["episodes_min"].fillna(0).sum()) / len(vom), 1),
                         "prn": None if not days else round(float(doses) / len(days), 1)}
        out.append(res)
    return out


# ---------------------------------------------------------------------------
# Inspector: edge cases (stored rows re-checked against the parser contract)
# ---------------------------------------------------------------------------

def flag_evidence(flag_id: int) -> list[dict]:
    """Rows a review flag points at, with their evidence span and extraction method."""
    df = query("""SELECT e.event_id, e.symptom_code, e.presence, e.evidence_span, e.extraction_method
                  FROM flag_events fe JOIN symptom_events e ON e.event_id = fe.event_id
                  WHERE fe.flag_id = ? AND fe.event_table = 'symptom' ORDER BY e.event_id""", [flag_id])
    return df.to_dict("records")


def report_texts(report_id: str) -> tuple[str, dict]:
    """(patient free text or questionnaire comment, questionnaire answers or {})."""
    r = query("SELECT raw_text, questionnaire_json FROM raw_reports WHERE report_id = ?", [report_id]).iloc[0]
    q = json.loads(r["questionnaire_json"]) if isinstance(r["questionnaire_json"], str) else {}
    text = r["raw_text"] if isinstance(r["raw_text"], str) else (q.get("comments") or "")
    return text, q


def _evidence_ok(span, method: str, text: str, q: dict) -> bool:
    if method == "questionnaire":           # evidence is "field=value" from the form
        if not isinstance(span, str) or "=" not in span:
            return False
        k, v = span.split("=", 1)
        return k in q and str(q[k]) == v
    return evidence_in_text(span, text)


def verify_report(report_id: str) -> dict:
    """Re-validate the stored rows of one report: every symptom / intake row must load into the
    Pydantic contract (cinv.models) and its evidence must be found verbatim in what the patient
    submitted (free text / comment, or the questionnaire field). Returns counts and any failures."""
    text, q = report_texts(report_id)
    ev = report_events(report_id)
    it = query("""SELECT med_id, med_text_raw, intake_status, dose_count, taken_date, part_of_day, evidence_span,
                         extraction_method, confidence FROM medication_intake_events WHERE report_id = ?""",
               [report_id])
    problems: list[str] = []
    for _, e in ev.iterrows():
        fields = {k: (None if pd.isna(e[k]) else e[k]) for k in
                  ("symptom_code", "presence", "severity_level", "severity_nrs", "episode_count_min",
                   "episode_count_max", "occurred_date", "part_of_day", "evidence_span", "extraction_method",
                   "confidence")}
        try:
            SymptomEvent(**fields)
        except ValidationError as err:
            problems.append(f"event {e['event_id']}: schema ({err.errors()[0]['msg']})")
        if not _evidence_ok(e["evidence_span"], e["extraction_method"], text, q):
            problems.append(f"event {e['event_id']}: evidence not found in the submission")
    for _, i in it.iterrows():
        try:
            IntakeEvent(med=None, med_text_raw=i["med_text_raw"], intake_status=i["intake_status"],
                        dose_count=int(i["dose_count"]), taken_date=i["taken_date"], part_of_day=i["part_of_day"],
                        evidence_span=i["evidence_span"], extraction_method=i["extraction_method"],
                        confidence=float(i["confidence"]))
        except ValidationError as err:
            problems.append(f"intake: schema ({err.errors()[0]['msg']})")
        if not _evidence_ok(i["evidence_span"], i["extraction_method"], text, q):
            problems.append("intake: evidence not found in the submission")
    return {"rows": len(ev) + len(it), "problems": problems, "ok": not problems and len(ev) + len(it) > 0}
