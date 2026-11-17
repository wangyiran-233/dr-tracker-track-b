"""Helpers shared by all parsers: context loaded from the DB, medication
name mapping, evidence-span verification and deterministic post-processing
guards that run after any parser (rule or LLM)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from .models import COUNTED, MAX_PLAUSIBLE_EPISODES, FlagCandidate, ParsedReport


def nrs_to_level(nrs: int) -> int:
    """0-10 -> 0-4. Bands follow a commonly used mild/moderate/severe split
    (1-3 / 4-6 / 7-10); the 7-8 vs 9-10 split is a project convention."""
    if nrs == 0:
        return 0
    if nrs <= 3:
        return 1
    if nrs <= 6:
        return 2
    if nrs <= 8:
        return 3
    return 4


@dataclass
class Context:
    """Read-only reference data the parsers need."""
    med_names: dict[str, list[str]]                       # generic -> [generic, brands...]
    orders: list[dict] = field(default_factory=list)
    # patient_id -> [(submitted_at, text)] sorted; text = free text or questionnaire comments
    report_texts: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @classmethod
    def from_db(cls, con: sqlite3.Connection) -> "Context":
        names = {}
        for generic, brands in con.execute("SELECT generic_name, brand_names FROM medications"):
            names[generic] = [generic] + json.loads(brands)
        orders = [dict(zip(("patient_id", "generic", "is_prn", "start_date", "end_date", "is_antiemetic"), r))
                  for r in con.execute("""SELECT o.patient_id, m.generic_name, o.is_prn, o.start_date,
                                                 o.end_date, m.is_antiemetic
                                          FROM medication_orders o JOIN medications m USING (med_id)""")]
        texts: dict[str, list[tuple[str, str]]] = {}
        for pid, sub, src, raw, qj in con.execute(
                "SELECT patient_id, submitted_at, source, raw_text, questionnaire_json FROM raw_reports "
                "ORDER BY submitted_at"):
            t = raw if src == "free_text" else (json.loads(qj).get("comments") or "").strip()
            if t:
                texts.setdefault(pid, []).append((sub, t))
        return cls(names, orders, texts)

    def previous_report(self, patient_id: str, submitted_at: str, max_hours: int) -> str | None:
        """Most recent earlier report text from the same patient within max_hours,
        formatted for the LLM as context (never extracted from)."""
        now = datetime.fromisoformat(submitted_at)
        for sub, text in reversed(self.report_texts.get(patient_id, [])):
            then = datetime.fromisoformat(sub)
            if then >= now:
                continue
            if (now - then).total_seconds() > max_hours * 3600:
                return None
            return f'(submitted {sub[:10]} {sub[11:16]}) "{text}"'
        return None

    def map_med(self, text: str | None) -> str | None:
        """Map a patient's wording to a generic name via whole-word match on
        generic/brand names. No fuzzy guessing: unknown wording -> None."""
        if not text:
            return None
        t = text.lower()
        hits = set()
        for generic, names in self.med_names.items():
            for n in names:
                if re.search(r"(?<![a-z0-9])" + re.escape(n.lower()) + r"(?![a-z0-9])", t):
                    hits.add(generic)
        return hits.pop() if len(hits) == 1 else None

    def active_prn_antiemetics(self, patient_id: str, on: date) -> list[str]:
        """All PRN antiemetics with an order active on that date. The caller only
        infers a medication when there is exactly one candidate."""
        d = on.isoformat()
        return sorted({o["generic"] for o in self.orders
                       if o["patient_id"] == patient_id and o["is_prn"] and o["is_antiemetic"]
                       and o["start_date"] <= d and (o["end_date"] is None or o["end_date"] >= d)})


def submitted_date(submitted_at: str) -> date:
    return datetime.fromisoformat(submitted_at).date()


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s)
    return s.strip().strip(" .,;:!?\"'")


def evidence_in_text(span: str | None, text: str) -> bool:
    """True if the evidence span appears verbatim in the report (case,
    whitespace, curly quotes and edge punctuation normalised)."""
    if not span or not span.strip():
        return False
    n = _norm(span)
    return bool(n) and n in _norm(text)


def postprocess(rep: ParsedReport) -> ParsedReport:
    """Deterministic guards applied after ANY parser, so conventions don't
    depend on the parser behaving:
      * absent -> severity 0, counted symptoms 0/0
      * present counted symptom without a number -> 1/NULL (at least one)
      * implausible counts -> 1/NULL + implausible_value flag
      * part_of_day known -> time_precision at least 'part_of_day'
    """
    for i, e in enumerate(rep.symptom_events):
        if e.presence == "absent":
            e.severity_level = 0
            if e.symptom_code in COUNTED:
                e.episode_count_min, e.episode_count_max = 0, 0
        elif e.symptom_code in COUNTED and e.presence == "present":
            if e.episode_count_min is None or e.episode_count_min == 0:
                e.episode_count_min = 1
                if e.episode_count_max == 0:
                    e.episode_count_max = None
            if (e.episode_count_min or 0) > MAX_PLAUSIBLE_EPISODES or \
                    (e.episode_count_max or 0) > MAX_PLAUSIBLE_EPISODES:
                rep.flags.append(FlagCandidate(
                    flag_type="implausible_value", severity="warning", report_id=rep.report_id,
                    description=f"{e.symptom_code} count {e.episode_count_min}-{e.episode_count_max} "
                                f"is implausible; stored as 'at least 1'. Evidence: {e.evidence_span!r}",
                    refs=[(rep.report_id, "symptom", i)]))
                e.episode_count_min, e.episode_count_max = 1, None
        if e.symptom_code not in COUNTED:
            e.episode_count_min = e.episode_count_max = None
        if e.occurred_start and e.part_of_day == "unknown":
            e.part_of_day = part_of_day_from_clock(e.occurred_start)
        if e.part_of_day not in ("unknown", "all_day") and e.time_precision == "day":
            e.time_precision = "part_of_day"
    for e in rep.intake_events:
        if e.taken_at and e.part_of_day == "unknown":
            e.part_of_day = part_of_day_from_clock(e.taken_at)
        if e.part_of_day not in ("unknown", "all_day") and e.time_precision == "day":
            e.time_precision = "part_of_day"
    return rep


def part_of_day_from_clock(iso: str) -> str:
    """Part of day from a clock time the PATIENT gave (never from submission time)."""
    h = int(iso[11:13])
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "overnight"
