"""One test per verification claim in the README."""

from __future__ import annotations

import sqlite3
from datetime import date

import groq
import httpx
import pytest

from cinv.common import Context
from cinv.llm import FatalLLMError
from conftest import add_questionnaire, add_report, llm_json, make_extractor, parse_with_llm, sym

PID = "P001"


def _flags(con, flag_type):
    return con.execute("SELECT flag_type, description, report_id FROM review_flags WHERE flag_type = ?",
                       (flag_type,)).fetchall()


# 1 - immutability triggers ---------------------------------------------------
def test_raw_reports_are_immutable(con):
    rid = con.execute("SELECT report_id FROM raw_reports LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        con.execute("UPDATE raw_reports SET raw_text = 'edited' WHERE report_id = ?", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        con.execute("DELETE FROM raw_reports WHERE report_id = ?", (rid,))
    con.execute("UPDATE raw_reports SET parse_status = 'parsed' WHERE report_id = ?", (rid,))   # allowed


# 2 - anti-hallucination guard --------------------------------------------------
def test_fabricated_evidence_is_dropped_and_flagged(con, tmp_path):
    text = "Felt a bit queasy after lunch."
    add_report(con, "T001", PID, "2026-09-01T18:00:00", text)
    ex, _ = make_extractor(tmp_path, {text: llm_json([
        sym("nausea", "a bit queasy", severity=1),                              # real quote -> kept
        sym("vomiting", "threw up twice in the night", count=(2, 2))]),         # not in the report -> dropped
    })
    parse_with_llm(con, ex, ["T001"])
    codes = [r[0] for r in con.execute("SELECT symptom_code FROM symptom_events WHERE report_id = 'T001'")]
    assert codes == ["nausea"]
    low = _flags(con, "low_confidence")
    assert len(low) == 1 and "evidence not in text" in low[0][1]
    assert con.execute("SELECT parse_status FROM raw_reports WHERE report_id = 'T001'").fetchone()[0] == "needs_review"


# 3 - deterministic guard overrides LLM exaggeration -----------------------------
def test_exaggerated_count_is_stored_as_1_null_and_flagged(con, tmp_path):
    text = "Threw up like a hundred times today."
    add_report(con, "T002", PID, "2026-09-01T18:00:00", text)
    ex, _ = make_extractor(tmp_path, {text: llm_json([
        sym("vomiting", "Threw up like a hundred times", count=(100, 100))])})   # the LLM copied it literally
    parse_with_llm(con, ex, ["T002"])
    assert con.execute("SELECT episode_count_min, episode_count_max FROM symptom_events "
                       "WHERE report_id = 'T002'").fetchall() == [(1, None)]
    assert len(_flags(con, "implausible_value")) == 1


# 4 - documented view aggregation rule -------------------------------------------
def test_conflicting_counts_same_part_of_day_take_max_and_flag(con, tmp_path):
    a, b = "Threw up twice this morning.", "Actually I threw up four times this morning."
    add_report(con, "T003", PID, "2026-09-01T12:00:00", a)
    add_report(con, "T004", PID, "2026-09-01T13:00:00", b)
    ex, _ = make_extractor(tmp_path, {
        a: llm_json([sym("vomiting", "Threw up twice this morning", count=(2, 2), part_of_day="morning")]),
        b: llm_json([sym("vomiting", "threw up four times this morning", count=(4, 4), part_of_day="morning")]),
    })
    parse_with_llm(con, ex, ["T003", "T004"])
    assert len(_flags(con, "cross_report_conflict")) == 1
    row = con.execute("""SELECT episodes_min, episodes_max, has_open_conflict FROM v_daily_symptom_summary
                         WHERE patient_id = ? AND occurred_date = '2026-09-01' AND symptom_code = 'vomiting'""",
                      (PID,)).fetchone()
    assert row == (4, 4, 1)          # max(2, 4), not 2 + 4, and the day is marked as disputed


# 5 - non-destructive audit trail --------------------------------------------------
def test_correction_supersedes_the_original_event(con, tmp_path):
    first, fix = "Threw up once yesterday.", "Correction: it was actually 3 times, not once."
    add_report(con, "T005", PID, "2026-09-02T09:00:00", first)
    add_report(con, "T006", PID, "2026-09-02T20:00:00", fix)
    ex, _ = make_extractor(tmp_path, {
        first: llm_json([sym("vomiting", "Threw up once yesterday", count=(1, 1), day_offset=-1)]),
        fix: llm_json([sym("vomiting", "it was actually 3 times", count=(3, 3), day_offset=-1)], is_correction=True),
    })
    parse_with_llm(con, ex, ["T005", "T006"])
    old = con.execute("SELECT event_id, status FROM symptom_events WHERE report_id = 'T005'").fetchone()
    new = con.execute("SELECT status, supersedes_event_id, episode_count_min FROM symptom_events "
                      "WHERE report_id = 'T006'").fetchone()
    assert old[1] == "superseded"                       # kept, not deleted
    assert new == ("active", old[0], 3)
    assert con.execute("SELECT episodes_min FROM v_daily_symptom_summary WHERE patient_id = ? AND "
                       "occurred_date = '2026-09-01' AND symptom_code = 'vomiting'", (PID,)).fetchone() == (3,)


# 6 - graceful failure handling -----------------------------------------------------
def test_transient_api_error_falls_back_to_rules_and_flags(con, tmp_path):
    bad, good = "Threw up twice overnight.", "Feeling a little nauseous today."
    add_report(con, "T007", PID, "2026-09-03T08:00:00", bad)
    add_report(con, "T008", PID, "2026-09-03T20:00:00", good)
    boom = groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.test"))
    ex, _ = make_extractor(tmp_path, {bad: boom, good: llm_json([sym("nausea", "a little nauseous", severity=1)])})
    parse_with_llm(con, ex, ["T007", "T008"])
    methods = dict(con.execute("SELECT report_id, extraction_method FROM symptom_events"))
    assert methods["T007"] == "rule"                    # fallback produced rows
    assert methods["T008"] == "llm"                     # and the run continued
    fb = [f for f in _flags(con, "low_confidence") if f[2] == "T007"]
    assert len(fb) == 1 and fb[0][1].startswith("LLM extraction failed")
    assert ex.fatal_error is None


@pytest.mark.parametrize("status, exc", [(401, groq.AuthenticationError), (429, groq.RateLimitError)])
def test_auth_or_quota_error_aborts_cleanly(con, tmp_path, status, exc):
    texts = [f"Threw up once, report number {i}." for i in range(3)]
    for i, t in enumerate(texts):
        add_report(con, f"T01{i}", PID, f"2026-09-04T{i + 8:02d}:00:00", t)
    err = exc("denied", response=httpx.Response(status, request=httpx.Request("POST", "https://api.groq.test")),
              body=None)
    ex, fake = make_extractor(tmp_path, {t: err for t in texts})
    before = con.execute("SELECT COUNT(*) FROM symptom_events").fetchone()[0]
    with pytest.raises(FatalLLMError):
        parse_with_llm(con, ex, ["T010", "T011", "T012"])
    assert fake.calls == 1                              # stopped at the first failure
    assert ex.fatal_error
    assert con.execute("SELECT COUNT(*) FROM symptom_events").fetchone()[0] == before   # nothing written
    assert not (tmp_path / "cache.json").exists()       # failures are never cached


# 7 - ambiguous PRN order branch ----------------------------------------------------
def test_several_active_prn_orders_leave_drug_null_and_flag(con, tmp_path):
    day = "2026-09-20"      # P001 has one active PRN order (ondansetron) from 2026-09-07
    ex, fake = make_extractor(tmp_path, {})             # a questionnaire without comments never calls the LLM
    ctx = Context.from_db(con)
    assert len(ctx.active_prn_antiemetics(PID, date.fromisoformat(day))) == 1       # control: one order -> inferred

    add_questionnaire(con, "T020", PID, f"{day}T20:00:00", {"nausea_0_10": 3, "prn_antiemetic_doses_24h": 2})
    parse_with_llm(con, ex, ["T020"])
    med_id, raw = con.execute("SELECT med_id, med_text_raw FROM medication_intake_events "
                              "WHERE report_id = 'T020'").fetchone()
    assert med_id is not None and raw.startswith("inferred")

    other = con.execute("SELECT med_id FROM medications WHERE is_antiemetic = 1 AND med_id NOT IN "
                        "(SELECT med_id FROM medication_orders WHERE patient_id = ? AND is_prn = 1) LIMIT 1",
                        (PID,)).fetchone()[0]
    con.execute("INSERT INTO medication_orders (patient_id, med_id, dose, route, frequency, is_prn, start_date) "
                "VALUES (?, ?, '10 mg', 'oral', 'as needed', 1, '2026-01-01')", (PID, other))
    con.commit()
    parse_with_llm(con, ex, ["T020"])                   # same report, now two active PRN antiemetic orders
    med_id, doses = con.execute("SELECT med_id, dose_count FROM medication_intake_events "
                                "WHERE report_id = 'T020'").fetchone()
    assert (med_id, doses) == (None, 2)
    flag = _flags(con, "unmapped_medication")
    assert len(flag) == 1 and "2 PRN antiemetic orders" in flag[0][1]
    assert fake.calls == 0
