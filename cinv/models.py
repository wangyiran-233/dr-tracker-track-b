"""Shared Pydantic models: the single output contract for every parser
(questionnaire rules, keyword baseline, Groq LLM). Field names mirror
sql/schema.sql so writing to SQLite is a straight mapping."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SymptomCode = Literal["nausea", "vomiting", "retching", "appetite_loss", "unable_to_keep_fluids"]
Presence = Literal["present", "absent", "uncertain"]
PartOfDay = Literal["overnight", "morning", "afternoon", "evening", "all_day", "unknown"]
TimePrecision = Literal["exact", "part_of_day", "day", "approximate"]
MedRelation = Literal["after_taking", "despite_taking", "before_taking", "vomited_up_dose"]
IntakeStatus = Literal["taken", "missed", "vomited_up", "unknown"]
Method = Literal["questionnaire", "rule", "llm", "manual"]
FlagType = Literal["intra_report_conflict", "cross_report_conflict", "severity_mismatch",
                   "low_confidence", "unmapped_medication", "implausible_value",
                   "duplicate_report", "red_flag",
                   # added by the deterministic flag layer (cinv/flag_layer.py)
                   "parser_disagreement", "distress", "other_symptom", "prompt_injection"]

SYMPTOMS: tuple[str, ...] = ("nausea", "vomiting", "retching", "appetite_loss", "unable_to_keep_fluids")
COUNTED = {"vomiting", "retching"}          # symptoms that have episode counts
LOW_CONFIDENCE = 0.6                       # below this an event is flagged for review
MAX_PLAUSIBLE_EPISODES = 20                # per report; above -> implausible_value
# med_text_raw marker for a medication name that was inferred (questionnaire PRN
# doses -> the single active PRN antiemetic order), not stated by the patient
INFERRED_PRN_TEXT = "inferred from active PRN order"


# (report_id, 'symptom' | 'intake' | 'weight', index within that report's list)
Ref = tuple[str, str, int]


class SymptomEvent(BaseModel):
    symptom_code: SymptomCode
    presence: Presence
    severity_level: int | None = Field(None, ge=0, le=4)
    severity_nrs: int | None = Field(None, ge=0, le=10)
    severity_raw: str | None = None
    episode_count_min: int | None = Field(None, ge=0)
    episode_count_max: int | None = Field(None, ge=0)
    occurred_date: date
    part_of_day: PartOfDay = "unknown"
    time_precision: TimePrecision = "day"
    timing_raw: str | None = None
    related_med: str | None = None          # generic name from the medication dictionary
    med_mention: str | None = None          # patient's own words for the medication
    med_relation: MedRelation | None = None
    evidence_span: str | None = None
    extraction_method: Method
    confidence: float = Field(1.0, ge=0, le=1)
    # pipeline bookkeeping (not produced by parsers)
    status: Literal["active", "superseded", "rejected"] = "active"
    supersedes: Ref | None = None

    @model_validator(mode="after")
    def _check(self):
        lo, hi = self.episode_count_min, self.episode_count_max
        if lo is not None and hi is not None and hi < lo:
            raise ValueError("episode_count_max < episode_count_min")
        if self.presence == "absent" and self.severity_level not in (None, 0):
            raise ValueError("absent symptom cannot have severity_level > 0")
        return self


class IntakeEvent(BaseModel):
    med: str | None                          # generic name; None = could not be mapped
    med_text_raw: str | None = None
    intake_status: IntakeStatus
    dose_count: int = Field(1, ge=0)
    taken_date: date
    part_of_day: PartOfDay = "unknown"
    time_precision: TimePrecision = "day"
    evidence_span: str | None = None
    extraction_method: Method
    confidence: float = Field(1.0, ge=0, le=1)


class WeightReading(BaseModel):
    weight_kg: float = Field(ge=20, le=300)
    measured_date: date
    evidence_span: str | None = None


class FlagCandidate(BaseModel):
    flag_type: FlagType
    severity: Literal["info", "warning", "urgent"] = "warning"
    description: str
    report_id: str                           # report the flag is filed under
    refs: list[Ref] = []


class ParsedReport(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    report_id: str
    patient_id: str
    source: Literal["free_text", "questionnaire"]
    method: Method                            # method used for the free text part
    symptom_events: list[SymptomEvent] = []
    intake_events: list[IntakeEvent] = []
    weights: list[WeightReading] = []
    is_correction: bool = False
    flags: list[FlagCandidate] = []
    dropped: list[str] = []                   # events rejected by validation, with reason
    failed: bool = False                      # parser could not produce output at all
