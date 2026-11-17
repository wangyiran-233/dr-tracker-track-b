"""Read-only data access for the dashboard (plus resolving review flags).

Every query goes through `query()`, which is cached and keyed on the DB file's
modification time, so re-parsing or resolving a flag invalidates the cache.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from cinv.models import INFERRED_PRN_TEXT
from run_queries import load_queries

ROOT = Path(__file__).resolve().parent.parent
# Default: the committed, pre-built demo database (LLM prompt v2, rebuilt from the cache).
# Override with CINV_DB to point the dashboard at another database, e.g. data/cinv.db.
DB = Path(os.environ.get("CINV_DB") or ROOT / "data" / "cinv_demo.db")
EVAL_DIR = ROOT / "eval"
QUERIES = load_queries(ROOT / "queries.sql")

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
        return "Database not found. Run `init_db.py` and `parse_reports.py --text-parser llm --offline` first, or check CINV_DB."
    n = query("SELECT COUNT(*) AS n FROM symptom_events")["n"].iloc[0]
    if n == 0:
        return "No parsed events yet. Run `parse_reports.py --text-parser rule` (or `llm`)."
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


def as_of(patient_id: str) -> date:
    v = query("SELECT MAX(date(submitted_at)) AS d FROM raw_reports WHERE patient_id = ?", [patient_id])["d"].iloc[0]
    return date.fromisoformat(v)


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
                       AND f.flag_type = 'red_flag') AS red_flags
             FROM raw_reports r"""
    params: list = []
    if patient_id:
        sql += " WHERE r.patient_id = ?"
        params.append(patient_id)
    return query(sql + " ORDER BY r.submitted_at DESC", params)


def report_events(report_id: str) -> pd.DataFrame:
    return query("""SELECT e.event_id, e.symptom_code, e.presence, e.severity_level, e.severity_nrs,
                           e.episode_count_min, e.episode_count_max, e.occurred_date, e.part_of_day,
                           e.occurred_start, e.time_precision,
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
                           i.intake_status, i.dose_count, i.taken_date,
                           CASE WHEN i.taken_at IS NULL THEN ''
                                ELSE (CASE i.time_precision WHEN 'approximate' THEN '~' ELSE '' END)
                                     || substr(i.taken_at, 12, 5) END AS clock_time,
                           i.part_of_day, i.evidence_span, i.confidence
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
# "Since last visit" comparison
# ---------------------------------------------------------------------------

def since_last_visit(patient_id: str) -> dict | None:
    """Last clinic visit = most recent infusion day (in this synthetic dataset
    visits coincide with infusions). Compares the days since that visit with
    the same days-of-cycle in the previous cycle."""
    ch = chemo(patient_id)
    end = pd.Timestamp(as_of(patient_id))
    past = ch[ch["d"] <= end]
    if past.empty:
        return None
    last = past.iloc[-1]
    n_days = int((end - last["d"]).days) + 1
    d = daily(patient_id)
    p = prn(patient_id)

    def window(start: pd.Timestamp) -> dict:
        stop = start + pd.Timedelta(days=n_days - 1)
        w = d[(d["occurred_date"] >= start) & (d["occurred_date"] <= stop)]
        nau = w[w["symptom_code"] == "nausea"]
        vom = w[w["symptom_code"] == "vomiting"]
        pw = p[(p["taken_date"] >= start) & (p["taken_date"] <= stop)]
        open_ended = bool(((vom["presence"] == "present") & vom["episodes_max"].isna()).any())
        return {
            "worst_nausea": None if nau["max_severity_level"].dropna().empty else int(nau["max_severity_level"].max()),
            "vomit_min": int(vom["episodes_min"].fillna(0).sum()),
            "vomit_open": open_ended,
            "prn": int(pw["doses"].sum()),
            "days_reported": int(w["occurred_date"].nunique()),
        }

    cur = window(last["d"])
    prev = None
    if len(past) >= 2:
        prev = window(past.iloc[-2]["d"])
    wt = weights(patient_id)
    w_now = wt[wt["measured_date"] <= end].tail(1)
    w_visit = wt[(wt["measured_date"] <= last["d"])].tail(1)
    fl = open_flags(patient_id)
    fl_since = fl[pd.to_datetime(fl["submitted_at"]) >= last["d"]]
    return {
        "visit_date": last["d"].date(), "cycle": int(last["cycle_number"]), "as_of": end.date(), "n_days": n_days,
        "current": cur, "previous": prev,
        "weight_now": None if w_now.empty else float(w_now["weight_kg"].iloc[0]),
        "weight_pct_baseline": None if w_now.empty else float(w_now["pct_change"].iloc[0]),
        "weight_below_alert": False if w_now.empty else bool(w_now["below_alert_line"].iloc[0]),
        "weight_at_visit": None if w_visit.empty else float(w_visit["weight_kg"].iloc[0]),
        "flags_since": len(fl_since), "red_since": int((fl_since["flag_type"] == "red_flag").sum()),
        "flags_open_total": len(fl),
    }
