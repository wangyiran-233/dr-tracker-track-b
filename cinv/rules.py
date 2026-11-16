"""Keyword / regex baseline for free-text reports.

Deliberately simple: it is the yardstick the LLM has to beat, and a fallback
when the LLM call fails. Works clause by clause (split on . ! ? , ; 'but').
Every event's evidence_span is the clause it came from.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .common import Context, submitted_date
from .models import IntakeEvent, ParsedReport, SymptomEvent, WeightReading

SYMPTOM_RX = {
    "nausea": r"nause\w*|queas\w*",
    "vomiting": r"threw (?:it )?up|throw(?:ing)? up|vomit\w*",
    "retching": r"retch\w*|dry heav\w*",
    "appetite_loss": r"appetite|\bate\b|\beat\b|eating",
    "unable_to_keep_fluids": r"(?:keep|kept|keeping) (?:any |anything )?(?:water|fluids?|liquids?|drinks?) down",
}
NEG_RX = r"\b(?:no|not|never|without|haven'?t|hasn'?t|couldn'?t|can'?t|didn'?t|gone|none)\b"
UNCERTAIN_RX = r"not sure|unsure|maybe|might be|kind of off|i think"
SEVERITY_RX = [  # checked from most to least severe
    (4, r"extreme\w*|unbearabl\w*|worst"),
    (3, r"severe\w*|\bvery\b|really|terribl\w*|awful"),
    (2, r"moderate\w*|pretty bad"),
    (1, r"mild\w*|slight\w*|a little|a bit|tiny bit"),
]
COUNT_WORDS = [
    (r"\bonce\b", (1, 1)), (r"\btwice\b", (2, 2)), (r"a couple(?: of)? times", (2, 2)),
    (r"a few times", (2, 4)), (r"several times", (3, None)), (r"\b(\d+) times\b", None),
]
PART_RX = [
    ("overnight", r"overnight|after midnight|during the night"),
    ("morning", r"morning|after breakfast|\bat (?:[5-9]|1[01])\b(?! ?pm)"),
    ("afternoon", r"afternoon|after lunch|at lunch|\b(?:1[2-7]|[1-5]) ?pm\b"),
    ("evening", r"evening|after dinner|tonight|last night"),
    ("all_day", r"all day"),
]
WEIGHT_RX = r"(\d{2,3}(?:\.\d+)?)\s*(kg|kgs|kilos?|lbs?|pounds)\b"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _clauses(sentence: str) -> list[str]:
    parts = re.split(r"[,;:]\s*(?:but\s+)?|\s+but\s+", sentence)
    return [p.strip() for p in parts if p and p.strip()]


def _day_offset(clause: str, report_default: int, morning_report: bool) -> int:
    c = clause.lower()
    if "day before yesterday" in c:
        return -2
    if re.search(r"yesterday|last night", c):
        return -1
    if morning_report and re.search(r"overnight|after midnight|during the night", c):
        return -1
    if re.search(r"today|tonight|this (?:morning|afternoon|evening)", c):
        return 0
    return report_default


def _part(clause: str) -> tuple[str, str | None]:
    c = clause.lower()
    for part, rx in PART_RX:
        m = re.search(rx, c)
        if m:
            return part, m.group(0)
    return "unknown", None


def _count(clause: str) -> tuple[int | None, int | None]:
    c = clause.lower()
    for rx, val in COUNT_WORDS:
        m = re.search(rx, c)
        if m:
            if val is None:
                n = int(m.group(1))
                return n, n
            return val
    return None, None


def parse_free_text_rule(report_id: str, patient_id: str, text: str, submitted_at: str,
                         ctx: Context) -> ParsedReport:
    base = submitted_date(submitted_at)
    morning_report = int(submitted_at[11:13]) < 12
    lower = text.lower()
    default_offset = -1 if re.search(r"update for yesterday|correction to yesterday", lower) else 0
    rep = ParsedReport(report_id=report_id, patient_id=patient_id, source="free_text", method="rule",
                       is_correction=bool(re.search(r"\bcorrection\b|\bactually\b", lower)))
    last_med: str | None = None
    seen: set[tuple] = set()
    despite_intakes: list[int] = []

    # a sentence without its own day word inherits the previous sentence's day
    clauses, carry = [], default_offset
    for s in _sentences(text):
        carry = _day_offset(s, carry, morning_report)
        clauses += [(cl, carry) for cl in _clauses(s)]
    for clause, sentence_offset in clauses:
        c = clause.lower()
        offset = _day_offset(clause, sentence_offset, morning_report)
        d = base + timedelta(days=offset)
        part, timing_raw = _part(clause)

        # --- medications mentioned in this clause
        med = ctx.map_med(clause)
        if med:
            last_med = med
        took = re.search(r"\btook\b|\btaking\b|\btake\b", c)
        if re.search(r"forgot|missed|skipped", c) and med:
            rep.intake_events.append(IntakeEvent(
                med=med, med_text_raw=clause, intake_status="missed", taken_date=d, part_of_day=part,
                evidence_span=clause, extraction_method="rule", confidence=0.7))
        elif took and (med or "pill" in c or "another" in c):
            m_ = med or (last_med if "another" in c else None)
            n_lo, _ = _count(clause)
            rep.intake_events.append(IntakeEvent(
                med=m_, med_text_raw=clause, intake_status="taken", dose_count=n_lo or 1, taken_date=d,
                part_of_day=part, evidence_span=clause, extraction_method="rule", confidence=0.7))
            if re.search(r"even after|despite", c):
                despite_intakes.append(len(rep.intake_events) - 1)
        if "threw it up" in c and rep.intake_events:
            rep.intake_events[-1].intake_status = "vomited_up"

        # --- weight
        wm = re.search(WEIGHT_RX, c)
        if wm:
            val = float(wm.group(1))
            kg = val if wm.group(2).startswith("k") else round(val * 0.4536, 1)
            if 20 <= kg <= 300:
                rep.weights.append(WeightReading(weight_kg=kg, measured_date=d, evidence_span=clause))

        # --- symptoms
        for code, rx in SYMPTOM_RX.items():
            m = re.search(rx, c)
            if not m:
                continue
            before = c[:m.start()]
            negated = bool(re.search(NEG_RX, before)) or (code != "unable_to_keep_fluids"
                                                          and re.search(r"\bgone\b", c))
            if code == "unable_to_keep_fluids":
                presence = "present" if re.search(NEG_RX, c) else "absent"
            elif code == "appetite_loss":
                if re.search(r"not much|poor|no appetite|low|couldn'?t eat|can'?t eat|barely", c):
                    presence = "present"
                elif re.search(r"okay|ok\b|fine|good|normal", c):
                    presence = "absent"
                else:
                    continue
            elif re.search(UNCERTAIN_RX, c) and code == "nausea" and not re.search(SEVERITY_RX[3][1], c):
                presence = "uncertain"
            else:
                presence = "absent" if negated else "present"

            level = None
            if code == "nausea" and presence == "present":
                for lvl, srx in SEVERITY_RX:
                    if re.search(srx, c):
                        level = lvl
                        break
            lo, hi = _count(clause) if code in ("vomiting", "retching") and presence == "present" else (None, None)
            related, relation = None, None
            if code == "vomiting" and "threw it up" in c and rep.intake_events:
                related, relation = rep.intake_events[-1].med, "vomited_up_dose"
            elif presence == "present" and re.search(r"even after|despite", c) and med:
                related, relation = med, "despite_taking"

            key = (code, d, part)
            if key in seen:
                continue
            seen.add(key)
            rep.symptom_events.append(SymptomEvent(
                symptom_code=code, presence=presence, severity_level=level,
                severity_raw=clause if level is not None else None,
                episode_count_min=lo, episode_count_max=hi, occurred_date=d, part_of_day=part,
                timing_raw=timing_raw, related_med=related, med_relation=relation, evidence_span=clause,
                extraction_method="rule", confidence=0.5 if presence == "uncertain" else 0.7))

    # "even after taking X" + a separate "took X N times" = one intake row (the counted one)
    for i in reversed(despite_intakes):
        e = rep.intake_events[i]
        if any(j != i and o.med == e.med and o.taken_date == e.taken_date and o.intake_status == "taken"
               for j, o in enumerate(rep.intake_events)):
            del rep.intake_events[i]
    return rep
