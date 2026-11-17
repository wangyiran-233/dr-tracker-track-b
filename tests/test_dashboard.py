"""UI smoke test: every dashboard page renders without an exception, for every patient,
against the committed demo database (data/cinv_demo.db)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = ROOT / "data" / "cinv_demo.db"
PAGES = ["page_overview", "page_trend", "page_cycles", "page_report", "page_method"]


def _patients() -> list[str]:
    con = sqlite3.connect(DEMO_DB)
    try:
        return [r[0] for r in con.execute("SELECT patient_id FROM patients ORDER BY 1")]
    finally:
        con.close()


@pytest.mark.parametrize("pid", _patients())
@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page, pid):
    src = (f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
           "import streamlit as st\nfrom dashboard import views\nviews.PAGES = {}\n"
           f"st.session_state['pid'] = {pid!r}\n"
           f"views.{page}()\n")
    at = AppTest.from_string(src, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [e for e in at.error if "Database not found" in e.value or "No parsed events" in e.value]
    assert len(at.title) == 1


def test_full_app_default_page():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert not [e for e in at.error if "Database not found" in e.value or "No parsed events" in e.value]
