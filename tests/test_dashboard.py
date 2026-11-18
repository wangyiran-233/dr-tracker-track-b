"""UI smoke tests against the committed demo database (data/cinv_demo.db): both clinical
views for every patient, the Timeline / Cycle-over-Cycle toggle, patient pills, the
Needs Attention cards, evidence tooltips, and the developer-only inspector toggle."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = ROOT / "data" / "cinv_demo.db"
CLINICAL = ["page_overview", "page_deepdive"]
INSPECTOR = ["page_report", "page_method"]
DB_ERRORS = ("Database not found", "No parsed events")


def _patients() -> list[str]:
    con = sqlite3.connect(DEMO_DB)
    try:
        return [r[0] for r in con.execute("SELECT patient_id FROM patients ORDER BY 1")]
    finally:
        con.close()


def _page(page: str, pid: str) -> AppTest:
    src = (f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
           "import streamlit as st\nfrom dashboard import views\nviews.PAGES = {}\n"
           f"if 'pid' not in st.session_state:\n    st.session_state['pid'] = {pid!r}\n"
           f"views.{page}()\n")
    return AppTest.from_string(src, default_timeout=60).run()


def _ok(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert not [e for e in at.error if any(m in e.value for m in DB_ERRORS)]


def _text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown) + "\n".join(c.value for c in at.caption)


@pytest.mark.parametrize("pid", _patients())
@pytest.mark.parametrize("page", CLINICAL)
def test_clinical_view_renders(page, pid):
    at = _page(page, pid)
    _ok(at)
    assert len(at.title) == 1


@pytest.mark.parametrize("page", INSPECTOR)
def test_inspector_page_renders(page):
    at = _page(page, "P002")
    _ok(at)
    assert len(at.title) == 1


def test_overview_needs_attention_cards():
    at = _page("page_overview", "P001")
    _ok(at)
    text = _text(at)
    for label in ("RED FLAG", "WEIGHT ALERT", "SILENT DROP-OFF", "Sorted by type"):
        assert label in text
    assert any(b.label == "Inspect Patient →" for b in at.button)
    assert "Open a patient in Patient Deep-Dive" in [p.label for p in at.pills]
    assert not at.selectbox                       # no patient dropdowns on clinical views


@pytest.mark.parametrize("pid", _patients())
def test_deepdive_cycle_toggle(pid):
    at = _page("page_deepdive", pid)
    assert at.segmented_control(key="dd_view").value == "Timeline"
    at.segmented_control(key="dd_view").set_value("Cycle-over-Cycle").run()
    _ok(at)
    assert "overlaid by day of cycle" in _text(at)
    at.segmented_control(key="dd_view").set_value("Timeline").run()
    _ok(at)
    assert "How to read" in _text(at)


def test_deepdive_patient_pills_and_regimen_change():
    at = _page("page_deepdive", "P001")
    assert not at.selectbox
    assert "Regimen change" not in _text(at)
    at.pills(key="pid_pick").set_value("P002").run()
    _ok(at)
    assert at.session_state["pid"] == "P002"
    text = _text(at)
    assert "Regimen change" in text and "olanzapine" in text
    assert "not evidence that the change caused" in text
    assert [m for m in at.metric if "before → after" in m.label]


def test_week_summary_wording_separates_zero_from_missing():
    import sys
    sys.path.insert(0, str(ROOT))
    from dashboard import data
    assert data.week_summary("P001")["vomiting"] == "0 episodes (reported none)"
    before_enrolment = data.week_summary("P001", "2026-09-01")
    assert before_enrolment["nausea"] == "not reported"
    assert before_enrolment["vomiting"] == "not reported"


def test_trend_tooltips_show_evidence_span():
    import sys
    sys.path.insert(0, str(ROOT))
    from dashboard import data
    from dashboard.charts import cycle_figure, trend_figure
    pid = "P002"
    ev = data.day_evidence(pid)
    p = data.patients().set_index("patient_id").loc[pid]
    figs = [trend_figure(data.daily(pid), data.prn(pid), data.weights(pid), data.chemo(pid), data.med_changes(pid),
                         data.flagged_days(pid), p["enrolled_at"], data.as_of(), ev=ev),
            cycle_figure(data.daily(pid), data.chemo(pid), data.med_changes(pid), 21, ev=ev)]
    for fig in figs:
        templates = [t.hovertemplate or "" for t in fig.data]
        assert any("Patient's words" in t and "Conflict / uncertainty" in t for t in templates)
        custom = " ".join(str(t.customdata.tolist()) for t in fig.data if t.customdata is not None)
        assert "Threw up 4 times this morning" in custom


def test_full_app_default_is_clinical_overview():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    _ok(at)
    assert at.title[0].value.startswith("Patients")
    assert at.toggle(key="dev_mode").value is False


def test_full_app_dev_toggle_and_jump_to_deepdive():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    at.toggle(key="dev_mode").set_value(True).run()
    _ok(at)
    assert at.toggle(key="dev_mode").value is True
    at.toggle(key="dev_mode").set_value(False).run()
    _ok(at)
    next(b for b in at.button if b.label == "Inspect Patient →").click().run()
    _ok(at)
    assert at.title[0].value == "Patient Deep-Dive"


def test_single_as_of_date_everywhere():
    """One dataset-wide as-of date: shown in the page header and used by the SQL snapshot."""
    import sys
    sys.path.insert(0, str(ROOT))
    from dashboard import data
    ref = data.as_of()
    snap = data.snapshot()
    assert set(snap["as_of"]) == {ref.isoformat()}
    last = data.last_reports().set_index("patient_id")["submitted_at"].str[:10]
    for _, r in snap.iterrows():
        assert r["days_since_last_report"] == (ref - __import__("datetime").date.fromisoformat(last[r["patient_id"]])).days
    for page in CLINICAL:
        at = _page(page, "P001")
        assert any(f"Data as of {ref:%b %d, %Y}" in i.value for i in at.info)


def test_method_page_reads_published_results():
    at = _page("page_method", "P001")
    _ok(at)
    assert not [c for c in at.caption if "not found" in c.value]
    assert any("LLM prompt v2" in m.value for m in at.markdown)
