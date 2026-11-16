"""Deterministic questionnaire parser. Answers are stored as given (the 0-10
score goes to severity_nrs unchanged); severity_level is derived with a fixed,
documented mapping. The free-text 'comments' field is handed to whichever
free-text parser is in use, then merged into the same report."""

from __future__ import annotations

import json
from typing import Callable

from .common import Context, nrs_to_level, submitted_date
from .models import FlagCandidate, IntakeEvent, ParsedReport, SymptomEvent, WeightReading

TextParser = Callable[[str, str, str, str, Context], ParsedReport]


def parse_questionnaire(report_id: str, patient_id: str, answers: dict | str, submitted_at: str,
                        ctx: Context, text_parser: TextParser) -> ParsedReport:
    q = json.loads(answers) if isinstance(answers, str) else answers
    d = submitted_date(submitted_at)
    rep = ParsedReport(report_id=report_id, patient_id=patient_id, source="questionnaire",
                       method="questionnaire")

    def ev(code, presence, field_name, value, **kw):
        rep.symptom_events.append(SymptomEvent(
            symptom_code=code, presence=presence, occurred_date=d, part_of_day="all_day",
            evidence_span=f"{field_name}={value}", extraction_method="questionnaire", confidence=1.0, **kw))

    n = q.get("nausea_0_10")
    if n is not None:
        ev("nausea", "present" if n > 0 else "absent", "nausea_0_10", n,
           severity_nrs=n, severity_level=nrs_to_level(n))
    v = q.get("vomit_episodes_24h")
    if v is not None:
        ev("vomiting", "present" if v > 0 else "absent", "vomit_episodes_24h", v,
           episode_count_min=v, episode_count_max=v)
    a = q.get("appetite_loss_0_10")
    if a is not None:
        ev("appetite_loss", "present" if a > 0 else "absent", "appetite_loss_0_10", a,
           severity_nrs=a, severity_level=nrs_to_level(a))
    f = q.get("kept_fluids_down")
    if f is not None:
        ev("unable_to_keep_fluids", "absent" if f == "yes" else "present", "kept_fluids_down", f,
           severity_raw=f"kept_fluids_down={f}")
        if f == "no":
            rep.flags.append(FlagCandidate(
                flag_type="red_flag", severity="urgent", report_id=report_id,
                description="Questionnaire: patient could not keep fluids down in the last 24h.",
                refs=[(report_id, "symptom", len(rep.symptom_events) - 1)]))

    prn = q.get("prn_antiemetic_doses_24h")
    if prn:
        med = ctx.active_prn_antiemetic(patient_id, d)
        rep.intake_events.append(IntakeEvent(
            med=med, med_text_raw="as-needed anti-nausea medicine (questionnaire)", intake_status="taken",
            dose_count=prn, taken_date=d, part_of_day="all_day",
            evidence_span=f"prn_antiemetic_doses_24h={prn}", extraction_method="questionnaire"))
        if med is None:
            rep.flags.append(FlagCandidate(
                flag_type="unmapped_medication", severity="info", report_id=report_id,
                description="PRN doses reported but no active PRN antiemetic order on that date.",
                refs=[(report_id, "intake", len(rep.intake_events) - 1)]))

    w = q.get("weight_kg")
    if w is not None:
        rep.weights.append(WeightReading(weight_kg=w, measured_date=d, evidence_span=f"weight_kg={w}"))

    comments = (q.get("comments") or "").strip()
    if comments:
        sub = text_parser(report_id, patient_id, comments, submitted_at, ctx)
        rep.method = sub.method
        offs_s, offs_i, offs_w = len(rep.symptom_events), len(rep.intake_events), len(rep.weights)
        rep.symptom_events += sub.symptom_events
        rep.intake_events += sub.intake_events
        rep.weights += sub.weights
        rep.is_correction = sub.is_correction
        rep.dropped += sub.dropped
        offs = {"symptom": offs_s, "intake": offs_i, "weight": offs_w}
        for fl in sub.flags:
            fl.refs = [(rid, kind, idx + offs[kind]) for rid, kind, idx in fl.refs]
            rep.flags.append(fl)
        rep.failed = sub.failed
    return rep
