"""Groq LLM extractor for free-text reports.

Division of labour (where the LLM is trusted vs. not):
  * LLM: reads language -> symptoms, presence, counts, severity words,
    time words, medication *mentions*, relative day offset.
  * Code: date arithmetic (day_offset + submission date), mapping medication
    mentions to the dictionary (no guessed drug names), all conventions in
    common.postprocess, and every flag.

Validation:
  1. JSON mode + Pydantic schema (one retry with the validation error fed back).
  2. evidence_span must appear verbatim in the report, otherwise the event is
     dropped (recorded in ParsedReport.dropped and flagged low_confidence).
  3. Self-reported confidence < LOW_CONFIDENCE -> low_confidence flag. This
     number is not calibrated, so it is a weak signal on top of 1-2, not a
     substitute for them.

Responses are cached on disk (keyed by model + prompt version + input), so
evaluation and re-runs don't spend API quota twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

from .common import Context, evidence_in_text, submitted_date
from .models import (IntakeEvent, IntakeStatus, MedRelation, ParsedReport, PartOfDay, Presence,
                     SymptomCode, SymptomEvent, WeightReading)

LATEST_PROMPT_VERSION = "v3"
# Largest open-weight model on the account's Groq model list (checked 2026-11-16).
# llama-3.3-70b-versatile is no longer served (404 model_not_found).
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_REASONING_EFFORT = "medium"     # gpt-oss / qwen3 are reasoning models: low | medium | high
# All cache entries written before effort became part of the cache key used this effort.
LEGACY_REASONING_EFFORT = "medium"
ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "llm_cache.json"


class FatalLLMError(RuntimeError):
    """Configuration-level failure (bad model id, auth, quota). Stop instead of
    silently falling back for every remaining report."""

SYSTEM_PROMPT_V1 = """You extract structured data from a chemotherapy patient's free-text symptom report.
Return ONLY a JSON object with this shape:
{"symptom_events":[{"symptom_code":..., "presence":..., "severity_level":int|null, "severity_raw":str|null,
  "episode_count_min":int|null, "episode_count_max":int|null, "day_offset":int, "part_of_day":...,
  "timing_raw":str|null, "med_mention":str|null, "med_relation":...|null, "evidence_span":str, "confidence":float}],
 "intake_events":[{"med_mention":str, "intake_status":..., "dose_count":int, "day_offset":int,
  "part_of_day":..., "evidence_span":str, "confidence":float}],
 "weights":[{"value":float, "unit":"kg"|"lb", "day_offset":int, "evidence_span":str}],
 "is_correction":bool}

Rules
- symptom_code: only nausea, vomiting, retching, appetite_loss, unable_to_keep_fluids. Ignore anything else.
- presence: "present"; "absent" when explicitly denied ("no vomiting", "ate okay" = appetite_loss absent);
  "uncertain" when the patient is unsure whether they had it. Unmentioned symptoms: no event.
- severity_level (nausea/appetite_loss only, else null): 1 mild/a little/slight/a bit/queasy-ish,
  2 moderate, 3 severe/very/really, 4 extreme/unbearable/worst ever. Relative words like "worse" or
  no intensity word -> null. Never guess. absent -> 0.
- episode counts (vomiting/retching only): "once" 1/1, "twice" 2/2, "N times" N/N, "a few times" 2/4,
  vomited but no number -> 1/null. Exaggerations ("a hundred times") -> 1/null.
- day_offset: 0 = submission day, -1 = yesterday, -2 = day before yesterday. "Last night" -> -1.
  Overnight/after-midnight episodes described in a MORNING report belong to the night that started
  the previous day -> -1, part_of_day "overnight".
- part_of_day: overnight, morning, afternoon, evening, all_day, unknown. after breakfast=morning,
  lunch/2pm=afternoon, dinner/tonight/last night=evening. No time words -> unknown.
- med_mention: copy the patient's exact words for a medication (e.g. "Zofran", "the little pill").
  Do NOT translate to a drug name. med_relation: despite_taking ("still nauseous even after X"),
  after_taking, before_taking, vomited_up_dose (vomited the pill back up).
- intake_events: each medication the patient took / missed / vomited up. intake_status: taken, missed,
  vomited_up, unknown. If a report says "even after taking X" and also "took X N times", output ONE
  intake row with dose_count N.
- weights: only explicit numbers.
- is_correction: true if the patient corrects something they reported earlier.
- evidence_span: an EXACT, contiguous substring copied from the report that supports the row.
  Do not paraphrase. Rows whose evidence is not found verbatim are discarded.
- confidence: 0-1, how sure you are the row is right.

Example
Report (submitted 2026-01-05 08:15, morning): "Vomited twice overnight. Still a little queasy, took a Compazine."
{"symptom_events":[{"symptom_code":"vomiting","presence":"present","severity_level":null,"severity_raw":null,
"episode_count_min":2,"episode_count_max":2,"day_offset":-1,"part_of_day":"overnight","timing_raw":"overnight",
"med_mention":null,"med_relation":null,"evidence_span":"Vomited twice overnight","confidence":0.95},
{"symptom_code":"nausea","presence":"present","severity_level":1,"severity_raw":"a little queasy",
"episode_count_min":null,"episode_count_max":null,"day_offset":0,"part_of_day":"unknown","timing_raw":null,
"med_mention":null,"med_relation":null,"evidence_span":"Still a little queasy","confidence":0.85}],
"intake_events":[{"med_mention":"Compazine","intake_status":"taken","dose_count":1,"day_offset":0,
"part_of_day":"unknown","evidence_span":"took a Compazine","confidence":0.9}],"weights":[],"is_correction":false}"""


# v2 changes vs v1 (motivated by v1 errors on the gold set, see eval/results_v1.md):
#  1. Exaggerated counts are copied literally; the deterministic guard in
#     common.postprocess turns them into 1/NULL AND raises implausible_value.
#     (v1 told the model to normalise them itself, which hid them from the guard.)
#  2. part_of_day only from time words in the text, never from the submission time.
#  3. The patient's previous report may be supplied as context, so corrections like
#     "it was actually 3 times" can be resolved. Extraction stays limited to the
#     current report; the verbatim evidence check drops anything copied from context.
_V2_EDITS = [
    ('vomited but no number -> 1/null. Exaggerations ("a hundred times") -> 1/null.',
     """vomited but no number -> 1/null. If the patient gives a number, copy it literally even if it
  sounds exaggerated ("a hundred times" -> 100/100); plausibility is checked downstream."""),
    ("No time words -> unknown.",
     """No time words -> unknown.
  Use ONLY time words written in the report. The submission time is given solely to resolve
  "today"/"yesterday"/"last night"; never use it to choose part_of_day."""),
    ("- is_correction: true if the patient corrects something they reported earlier.",
     """- is_correction: true if the patient corrects something they reported earlier.
- CONTEXT: you may be given the patient's previous report. Use it only to understand references in the
  current report (e.g. "it was actually 3 times" = the symptom mentioned before). Extract events ONLY
  from the current report; every evidence_span must be copied from the current report."""),
]
SYSTEM_PROMPT_V2 = SYSTEM_PROMPT_V1
for _old, _new in _V2_EDITS:
    assert _old in SYSTEM_PROMPT_V2, _old
    SYSTEM_PROMPT_V2 = SYSTEM_PROMPT_V2.replace(_old, _new)

# v3 changes vs v2 (motivated by the full v2 run and the human review of the gold set):
#  1. Evidence: quote the whole contiguous phrase when supporting words are not adjacent
#     ("no nausea or vomiting"); one span may support several rows. The verbatim check is
#     unchanged (R0043: 20 correct "vomiting absent" rows were dropped on the full run).
#  2. Clock times the patient states, or that follow arithmetically from a stated time, are
#     returned as clock_time (+ approximate flag); never derived from submission time (R0003, R0067).
#  3. Explicit day rule for sentences without a day word: inherit the previous sentence's day,
#     never its part of day (R0034/R0037, R0175).
_V3_EDITS = [
    ('"day_offset":int, "part_of_day":...,',
     '"day_offset":int, "part_of_day":..., "clock_time":"HH:MM"|null, "clock_time_approximate":bool,'),
    ('  "part_of_day":..., "evidence_span":str, "confidence":float}],',
     '  "part_of_day":..., "clock_time":"HH:MM"|null, "clock_time_approximate":bool, "evidence_span":str,\n'
     '  "confidence":float}],'),
    ("""  the previous day -> -1, part_of_day "overnight".""",
     """  the previous day -> -1, part_of_day "overnight".
  A sentence with no day word of its own ("today", "this morning", "tonight" count as day words for
  day 0) takes the day of the previous sentence in the same report: "Yesterday was a good day, no
  nausea. Appetite is okay." -> appetite day_offset -1. If no earlier sentence has a day word, use 0.
  Inherit ONLY the day, never part_of_day."""),
    ("""- med_mention: copy the patient's exact words""",
     """- clock_time: only when the patient states a clock time ("at 8", "around 2pm") or one follows by simple
  arithmetic from a stated time ("took it at 8 ... threw it up 20 minutes later" -> "08:20"). Use 24 h
  "HH:MM". clock_time_approximate: true for "around/about/~" and for computed times, false for a plainly
  stated time. Never derive a clock time from the submission time or from words like "morning". Else null.
- med_mention: copy the patient's exact words"""),
    ("""  Do not paraphrase. Rows whose evidence is not found verbatim are discarded.""",
     """  Do not paraphrase. Rows whose evidence is not found verbatim are discarded.
  If the words supporting a row are not adjacent (e.g. vomiting absent in "no nausea or vomiting"), quote
  the whole contiguous phrase that contains them ("no nausea or vomiting"); never join fragments.
  The same evidence_span may support several rows."""),
]
SYSTEM_PROMPT_V3 = SYSTEM_PROMPT_V2
for _old, _new in _V3_EDITS:
    assert SYSTEM_PROMPT_V3.count(_old) == 1, _old
    SYSTEM_PROMPT_V3 = SYSTEM_PROMPT_V3.replace(_old, _new)

PROMPTS = {"v1": SYSTEM_PROMPT_V1, "v2": SYSTEM_PROMPT_V2, "v3": SYSTEM_PROMPT_V3}
CONTEXT_WINDOW_HOURS = 72

# --- LLM output contract (validated with Pydantic) -------------------------

def _norm_clock(v: str | None) -> str | None:
    """Accept 'H:MM' or 'HH:MM' (24 h), return 'HH:MM'; anything else is a validation error."""
    if v is None or v == "":
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", v)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"clock_time must be HH:MM (24 h), got {v!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


class LLMSymptom(BaseModel):
    symptom_code: SymptomCode
    presence: Presence
    severity_level: int | None = Field(None, ge=0, le=4)
    severity_raw: str | None = None
    episode_count_min: int | None = Field(None, ge=0)
    episode_count_max: int | None = Field(None, ge=0)
    day_offset: int = Field(0, ge=-7, le=0)
    part_of_day: PartOfDay = "unknown"
    clock_time: str | None = None              # v3: "HH:MM", only when stated / computed from a stated time
    clock_time_approximate: bool = False
    timing_raw: str | None = None
    med_mention: str | None = None
    med_relation: MedRelation | None = None
    evidence_span: str
    confidence: float = Field(ge=0, le=1)

    _clock = field_validator("clock_time")(lambda cls, v: _norm_clock(v))


class LLMIntake(BaseModel):
    med_mention: str
    intake_status: IntakeStatus
    dose_count: int = Field(1, ge=0, le=20)
    day_offset: int = Field(0, ge=-7, le=0)
    part_of_day: PartOfDay = "unknown"
    clock_time: str | None = None
    clock_time_approximate: bool = False
    evidence_span: str
    confidence: float = Field(ge=0, le=1)

    _clock = field_validator("clock_time")(lambda cls, v: _norm_clock(v))


class LLMWeight(BaseModel):
    value: float = Field(gt=0)
    unit: Literal["kg", "lb"]
    day_offset: int = Field(0, ge=-7, le=0)
    evidence_span: str


class LLMExtraction(BaseModel):
    symptom_events: list[LLMSymptom] = []
    intake_events: list[LLMIntake] = []
    weights: list[LLMWeight] = []
    is_correction: bool = False


# --- client ---------------------------------------------------------------

class GroqExtractor:
    def __init__(self, model: str | None = None, cache_path: Path = CACHE_PATH, offline: bool = False,
                 prompt_version: str | None = None):
        load_dotenv(ROOT / ".env")
        self.model = model or os.getenv("GROQ_MODEL") or DEFAULT_MODEL
        self.reasoning_effort = os.getenv("GROQ_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT
        self.prompt_version = prompt_version or os.getenv("GROQ_PROMPT_VERSION") or LATEST_PROMPT_VERSION
        if self.prompt_version not in PROMPTS:
            raise ValueError(f"unknown prompt version {self.prompt_version!r}")
        self.cache_path = cache_path
        self.cache = self._load_cache()
        self.client = None
        self.calls = 0              # HTTP requests that returned a completion
        self.fatal_error: str | None = None
        key = os.getenv("GROQ_API_KEY")
        if not offline and key:
            from groq import Groq
            # SDK retries 429/5xx with backoff (honours retry-after)
            self.client = Groq(api_key=key, max_retries=6)

    @property
    def available(self) -> bool:
        return self.client is not None

    def _is_reasoning_model(self) -> bool:
        return self.model.startswith(("openai/gpt-oss", "qwen/"))

    def _key(self, text: str, submitted_at: str, context: str | None = None) -> str:
        """Cache key = everything that changes the model's answer.
        Backward compatible: entries written before reasoning effort was part
        of the key were all produced at LEGACY_REASONING_EFFORT, so that
        effort adds no suffix and those entries stay valid."""
        raw = f"{self.model}|{self.prompt_version}|{submitted_at}|{text}"
        if context:          # v1 keys (no context) stay unchanged
            raw += f"|ctx:{context}"
        if self._is_reasoning_model() and self.reasoning_effort != LEGACY_REASONING_EFFORT:
            raw += f"|effort:{self.reasoning_effort}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _load_cache(self) -> dict[str, str]:
        """Only schema-valid completions belong in the cache; anything else
        (e.g. an error payload from an older version) is dropped on load."""
        if not self.cache_path.exists():
            return {}
        raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        good = {}
        for k, v in raw.items():
            try:
                LLMExtraction.model_validate_json(v)
                good[k] = v
            except (ValidationError, TypeError):
                pass
        if len(good) != len(raw):
            print(f"llm cache: removed {len(raw) - len(good)} invalid entr(ies)")
            self.cache = good
            self._save_cache()
        return good

    def is_cached(self, text: str, submitted_at: str, context: str | None = None) -> bool:
        return self._key(text, submitted_at, context) in self.cache

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _user_msg(self, text: str, submitted_at: str, context: str | None = None) -> str:
        hour = int(submitted_at[11:13])
        when = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")
        msg = f'Report (submitted {submitted_at[:10]} {submitted_at[11:16]}, {when}): "{text}"'
        if self.prompt_version == "v1" or not context:
            return msg
        return ("Previous report from this patient (CONTEXT ONLY, do not extract from it): "
                f"{context}\nCurrent {msg}")

    def extract(self, text: str, submitted_at: str,
                context: str | None = None) -> tuple[LLMExtraction | None, str | None]:
        """Returns (validated extraction, error). Uses the cache first.
        Only schema-valid completions are ever written to the cache."""
        if self.prompt_version == "v1":
            context = None
        key = self._key(text, submitted_at, context)
        if key in self.cache:
            try:
                return LLMExtraction.model_validate_json(self.cache[key]), None
            except ValidationError as e:
                return None, f"cached response invalid: {e.error_count()} errors"
        if self.fatal_error:
            return None, self.fatal_error
        if not self.available:
            return None, "no GROQ_API_KEY and no cached response"

        import groq
        extra = {}
        if self._is_reasoning_model():
            extra = {"reasoning_effort": self.reasoning_effort, "include_reasoning": False}
        messages = [{"role": "system", "content": PROMPTS[self.prompt_version]},
                    {"role": "user", "content": self._user_msg(text, submitted_at, context)}]
        last_err = None
        for _attempt in range(2):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0,
                    response_format={"type": "json_object"}, max_completion_tokens=6000, **extra)
            except (groq.NotFoundError, groq.AuthenticationError, groq.PermissionDeniedError,
                    groq.RateLimitError) as e:
                # wrong model id / bad key / quota still exhausted after SDK retries:
                # every further call would fail the same way -> stop.
                self.fatal_error = f"Groq API error: {type(e).__name__}: {e}"
                return None, self.fatal_error
            except groq.BadRequestError as e:
                # e.g. 'json_validate_failed': model emitted invalid JSON -> retry once
                last_err = f"{type(e).__name__}: {str(e)[:300]}"
                messages = messages[:2] + [{"role": "user", "content": "Return one valid JSON object only."}]
                continue
            except Exception as e:  # network etc. -> this report only
                return None, f"Groq API error: {type(e).__name__}: {e}"
            self.calls += 1
            content = resp.choices[0].message.content or ""
            try:
                parsed = LLMExtraction.model_validate_json(content)
                self.cache[key] = content
                self._save_cache()
                return parsed, None
            except ValidationError as e:
                last_err = str(e)[:1500]
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": "That JSON failed validation:\n" + last_err +
                                                         "\nReturn the corrected JSON object only."}]
        return None, f"no valid output after 2 attempts: {last_err[:300] if last_err else ''}"


def _clock_fields(day, clock: str | None, approximate: bool) -> tuple[str | None, str]:
    """(ISO datetime or None, time_precision). 'day' is refined to 'part_of_day' later
    by postprocess when a part of day is known."""
    if not clock:
        return None, "day"
    return f"{day.isoformat()}T{clock}:00", ("approximate" if approximate else "exact")


def parse_free_text_llm(extractor: GroqExtractor):
    """Build a text-parser function with the same signature as the rule parser."""

    def _parse(report_id: str, patient_id: str, text: str, submitted_at: str, ctx: Context) -> ParsedReport:
        rep = ParsedReport(report_id=report_id, patient_id=patient_id, source="free_text", method="llm")
        context = ctx.previous_report(patient_id, submitted_at, CONTEXT_WINDOW_HOURS)
        out, err = extractor.extract(text, submitted_at, context)
        if out is None:
            rep.failed = True
            rep.dropped.append(err or "unknown error")
            return rep
        base = submitted_date(submitted_at)
        rep.is_correction = out.is_correction

        for s in out.symptom_events:
            if not evidence_in_text(s.evidence_span, text):
                rep.dropped.append(f"symptom {s.symptom_code}: evidence not in text: {s.evidence_span!r}")
                continue
            med = ctx.map_med(s.med_mention)
            day = base + timedelta(days=s.day_offset)
            start, precision = _clock_fields(day, s.clock_time, s.clock_time_approximate)
            try:
                rep.symptom_events.append(SymptomEvent(
                    symptom_code=s.symptom_code, presence=s.presence, severity_level=s.severity_level,
                    severity_raw=s.severity_raw, episode_count_min=s.episode_count_min,
                    episode_count_max=s.episode_count_max, occurred_date=day,
                    occurred_start=start, time_precision=precision,
                    part_of_day=s.part_of_day, timing_raw=s.timing_raw, related_med=med,
                    med_mention=s.med_mention, med_relation=s.med_relation, evidence_span=s.evidence_span,
                    extraction_method="llm", confidence=s.confidence))
            except ValidationError as e:
                rep.dropped.append(f"symptom {s.symptom_code}: inconsistent fields: {e.errors()[0]['msg']}")

        for i in out.intake_events:
            if not evidence_in_text(i.evidence_span, text):
                rep.dropped.append(f"intake {i.med_mention!r}: evidence not in text: {i.evidence_span!r}")
                continue
            day = base + timedelta(days=i.day_offset)
            taken_at, precision = _clock_fields(day, i.clock_time, i.clock_time_approximate)
            rep.intake_events.append(IntakeEvent(
                med=ctx.map_med(i.med_mention), med_text_raw=i.med_mention, intake_status=i.intake_status,
                dose_count=i.dose_count, taken_date=day, taken_at=taken_at, time_precision=precision,
                part_of_day=i.part_of_day, evidence_span=i.evidence_span, extraction_method="llm",
                confidence=i.confidence))

        for w in out.weights:
            if not evidence_in_text(w.evidence_span, text):
                rep.dropped.append(f"weight {w.value}: evidence not in text: {w.evidence_span!r}")
                continue
            kg = w.value if w.unit == "kg" else round(w.value * 0.4536, 1)
            try:
                rep.weights.append(WeightReading(weight_kg=kg, measured_date=base + timedelta(days=w.day_offset),
                                                 evidence_span=w.evidence_span))
            except ValidationError:
                rep.dropped.append(f"weight {w.value} {w.unit}: out of range")
        return rep

    return _parse
