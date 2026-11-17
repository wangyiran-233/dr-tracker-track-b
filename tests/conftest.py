"""Shared fixtures. Every test builds its own throw-away database from the
synthetic seed, so nothing touches data/ and no API key or network is needed."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import init_db  # noqa: E402
from cinv.common import Context  # noqa: E402
from cinv.llm import GroqExtractor, parse_free_text_llm  # noqa: E402
from cinv.pipeline import load_rows, run, write_results  # noqa: E402


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """No test may ever reach the real API."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture
def con(tmp_path):
    """Fresh database: schema + reference data + the 184 synthetic raw reports."""
    path = tmp_path / "t.db"
    init_db.build(path, init_db.DEFAULT_SEED)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def add_report(con, report_id: str, patient_id: str, submitted_at: str, text: str) -> None:
    con.execute("INSERT INTO raw_reports (report_id, patient_id, submitted_at, source, raw_text) "
                "VALUES (?, ?, ?, 'free_text', ?)", (report_id, patient_id, submitted_at, text))
    con.commit()


def add_questionnaire(con, report_id: str, patient_id: str, submitted_at: str, answers: dict) -> None:
    con.execute("INSERT INTO raw_reports (report_id, patient_id, submitted_at, source, questionnaire_json) "
                "VALUES (?, ?, ?, 'questionnaire', ?)", (report_id, patient_id, submitted_at, json.dumps(answers)))
    con.commit()


class FakeGroq:
    """Stands in for the Groq client. `script` maps a snippet of the current report's text to a JSON
    string (returned) or an Exception (raised). Counts calls so tests can prove no more were made."""

    def __init__(self, script: dict):
        self.script = script
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        user = kw["messages"][1]["content"]
        current = user.rsplit("Report (submitted", 1)[-1]      # the previous report (context) comes first
        for snippet, outcome in self.script.items():
            if snippet in current:
                if isinstance(outcome, Exception):
                    raise outcome
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])
        raise AssertionError(f"unscripted report: {current!r}")


def make_extractor(tmp_path, script: dict) -> tuple[GroqExtractor, FakeGroq]:
    ex = GroqExtractor(cache_path=tmp_path / "cache.json", offline=True)
    fake = FakeGroq(script)
    ex.client = fake
    return ex, fake


def parse_with_llm(con, ex, report_ids: list[str]):
    """Run the real pipeline (parse, flags, corrections, write) for the given reports."""
    ctx = Context.from_db(con)
    rows = load_rows(con, report_ids)
    parsed, cross = run(rows, ctx, parse_free_text_llm(ex), should_stop=lambda: ex.fatal_error)
    write_results(con, parsed, cross, "llm", ex.model, ex.prompt_version)
    return parsed


def llm_json(symptoms=(), intakes=(), is_correction=False) -> str:
    return json.dumps({"symptom_events": list(symptoms), "intake_events": list(intakes),
                       "weights": [], "is_correction": is_correction})


def sym(code, evidence, *, presence="present", count=None, day_offset=0, part_of_day="unknown",
        severity=None, confidence=0.9) -> dict:
    return {"symptom_code": code, "presence": presence, "severity_level": severity, "severity_raw": None,
            "episode_count_min": None if count is None else count[0],
            "episode_count_max": None if count is None else count[1],
            "day_offset": day_offset, "part_of_day": part_of_day, "timing_raw": None, "med_mention": None,
            "med_relation": None, "evidence_span": evidence, "confidence": confidence}
