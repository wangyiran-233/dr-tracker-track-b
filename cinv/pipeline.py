"""Pipeline: parse -> postprocess -> per-report flags -> corrections ->
cross-report checks -> write to SQLite.

All flag logic is deterministic code, never the LLM."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Callable

from .common import Context, postprocess
from .llm import FatalLLMError
from .models import COUNTED, LOW_CONFIDENCE, FlagCandidate, ParsedReport, SymptomEvent
from .questionnaire import parse_questionnaire
from .rules import parse_free_text_rule

TextParser = Callable[[str, str, str, str, Context], ParsedReport]
BROAD = {"all_day", "unknown"}


# ---------------------------------------------------------------------------
# Parsing one report
# ---------------------------------------------------------------------------

def with_rule_fallback(text_parser: TextParser) -> TextParser:
    """If the primary parser fails outright (API error, invalid JSON twice),
    fall back to the keyword rules and flag the report for review."""
    if text_parser is parse_free_text_rule:
        return text_parser

    def _wrapped(report_id, patient_id, text, submitted_at, ctx):
        rep = text_parser(report_id, patient_id, text, submitted_at, ctx)
        if not rep.failed:
            return rep
        fb = parse_free_text_rule(report_id, patient_id, text, submitted_at, ctx)
        fb.dropped = rep.dropped
        fb.flags.append(FlagCandidate(
            flag_type="low_confidence", severity="warning", report_id=report_id,
            description="LLM extraction failed (" + "; ".join(rep.dropped)[:200] +
                        "). Keyword-rule fallback used; please verify.",
            refs=[(report_id, "symptom", i) for i in range(len(fb.symptom_events))]))
        return fb

    return _wrapped


def parse_report(row: dict, ctx: Context, text_parser: TextParser, fallback: bool = True) -> ParsedReport:
    tp = with_rule_fallback(text_parser) if fallback else text_parser
    if row["source"] == "questionnaire":
        rep = parse_questionnaire(row["report_id"], row["patient_id"], row["questionnaire_json"],
                                  row["submitted_at"], ctx, tp)
    else:
        rep = tp(row["report_id"], row["patient_id"], row["raw_text"], row["submitted_at"], ctx)
    postprocess(rep)
    rep.flags = dedupe_flags(rep.flags + report_flags(rep))
    return rep


def dedupe_flags(flags: list[FlagCandidate]) -> list[FlagCandidate]:
    """One flag per (report, type, description): e.g. an unmapped medication
    mentioned by both an intake row and a symptom row is one review item that
    points at both rows."""
    merged: dict[tuple, FlagCandidate] = {}
    for f in flags:
        key = (f.report_id, f.flag_type, f.description)
        if key in merged:
            merged[key].refs = list(dict.fromkeys(merged[key].refs + f.refs))
        else:
            merged[key] = f
    return list(merged.values())


# ---------------------------------------------------------------------------
# Conflict logic (shared by intra- and cross-report checks)
# ---------------------------------------------------------------------------

def conflict_reason(a: SymptomEvent, b: SymptomEvent) -> str | None:
    """Do two events about the same symptom/day contradict each other?
    Same time window: presence or exact counts differ. Broad window
    (all_day/unknown) vs a specific part of day: the broad one says absent
    while the specific one says present, or the specific count exceeds the
    broad maximum."""
    if a.symptom_code != b.symptom_code or a.occurred_date != b.occurred_date:
        return None
    if "uncertain" in (a.presence, b.presence):
        return None
    pa, pb = a.part_of_day, b.part_of_day
    if pa == pb or (pa in BROAD and pb in BROAD):
        if a.presence != b.presence:
            return f"{a.presence} vs {b.presence}"
        if a.symptom_code in COUNTED and a.presence == "present" and \
                (a.episode_count_min, a.episode_count_max) != (b.episode_count_min, b.episode_count_max):
            return (f"count {a.episode_count_min}-{a.episode_count_max} vs "
                    f"{b.episode_count_min}-{b.episode_count_max}")
        if a.severity_level is not None and b.severity_level is not None and \
                abs(a.severity_level - b.severity_level) >= 2:
            return f"severity {a.severity_level} vs {b.severity_level}"
        return None
    if pa in BROAD or pb in BROAD:
        broad, spec = (a, b) if pa in BROAD else (b, a)
        if broad.presence == "absent" and spec.presence == "present":
            return f"{broad.part_of_day}: absent vs {spec.part_of_day}: present"
        if broad.episode_count_max is not None and spec.episode_count_min is not None and \
                spec.episode_count_min > broad.episode_count_max:
            return (f"{spec.part_of_day} count {spec.episode_count_min} > "
                    f"{broad.part_of_day} max {broad.episode_count_max}")
    return None


def report_flags(rep: ParsedReport) -> list[FlagCandidate]:
    rid, out = rep.report_id, []
    ev = rep.symptom_events

    low = [i for i, e in enumerate(ev) if e.confidence < LOW_CONFIDENCE]
    low_i = [i for i, e in enumerate(rep.intake_events) if e.confidence < LOW_CONFIDENCE]
    if low or low_i:
        out.append(FlagCandidate(
            flag_type="low_confidence", severity="info", report_id=rid,
            description=f"{len(low) + len(low_i)} extracted row(s) below confidence {LOW_CONFIDENCE}.",
            refs=[(rid, "symptom", i) for i in low] + [(rid, "intake", i) for i in low_i]))
    if rep.dropped and not any(f.flag_type == "low_confidence" and "fallback" in f.description
                               for f in rep.flags):
        out.append(FlagCandidate(
            flag_type="low_confidence", severity="warning", report_id=rid,
            description="Rows rejected by validation: " + " | ".join(rep.dropped)[:400]))

    for i, e in enumerate(rep.intake_events):
        if e.med is None and e.extraction_method != "questionnaire":
            out.append(FlagCandidate(
                flag_type="unmapped_medication", severity="info", report_id=rid,
                description=f"Medication {e.med_text_raw!r} not matched to the dictionary.",
                refs=[(rid, "intake", i)]))
    for i, e in enumerate(ev):
        if e.med_mention and e.related_med is None:
            out.append(FlagCandidate(
                flag_type="unmapped_medication", severity="info", report_id=rid,
                description=f"Medication {e.med_mention!r} not matched to the dictionary.",
                refs=[(rid, "symptom", i)]))

    for i, e in enumerate(ev):
        if e.symptom_code == "unable_to_keep_fluids" and e.presence == "present" and \
                e.extraction_method != "questionnaire":     # questionnaire 'no' is flagged by its parser
            out.append(FlagCandidate(
                flag_type="red_flag", severity="urgent", report_id=rid,
                description=f"Patient reports not keeping fluids down: {e.evidence_span!r}",
                refs=[(rid, "symptom", i)]))

    for i, e in enumerate(ev):
        if e.symptom_code == "nausea" and e.presence == "present" and e.severity_level is not None \
                and e.severity_level <= 1:
            heavy = [j for j, o in enumerate(ev) if o.occurred_date == e.occurred_date and o.presence == "present"
                     and (o.symptom_code == "unable_to_keep_fluids"
                          or (o.symptom_code == "vomiting" and (o.episode_count_min or 0) >= 4))]
            if heavy:
                out.append(FlagCandidate(
                    flag_type="severity_mismatch", severity="warning", report_id=rid,
                    description="Nausea described as mild, but same day: " +
                                ", ".join(ev[j].symptom_code for j in heavy) + ".",
                    refs=[(rid, "symptom", i)] + [(rid, "symptom", j) for j in heavy]))

    for i in range(len(ev)):
        for j in range(i + 1, len(ev)):
            why = conflict_reason(ev[i], ev[j])
            if why:
                out.append(FlagCandidate(
                    flag_type="intra_report_conflict", severity="warning", report_id=rid,
                    description=f"{ev[i].symptom_code} on {ev[i].occurred_date}: {why} within one report.",
                    refs=[(rid, "symptom", i), (rid, "symptom", j)]))
    return out


# ---------------------------------------------------------------------------
# Across reports
# ---------------------------------------------------------------------------

def _content_key(row: dict) -> str:
    if row["source"] == "free_text":
        return re.sub(r"\s+", " ", row["raw_text"].strip().lower())
    q = row["questionnaire_json"]
    return json.dumps(json.loads(q) if isinstance(q, str) else q, sort_keys=True)


def cross_report_pass(rows: list[dict], parsed: dict[str, ParsedReport]) -> list[FlagCandidate]:
    """Rows must be sorted by submitted_at. Applies corrections (supersedes
    older events) then flags duplicates and cross-report conflicts."""
    flags: list[FlagCandidate] = []
    by_patient: dict[str, list[dict]] = {}
    for r in rows:
        by_patient.setdefault(r["patient_id"], []).append(r)

    for patient_rows in by_patient.values():
        # corrections
        for k, r in enumerate(patient_rows):
            rep = parsed[r["report_id"]]
            if not rep.is_correction:
                continue
            for i, e in enumerate(rep.symptom_events):
                for prev in reversed(patient_rows[:k]):
                    prep = parsed[prev["report_id"]]
                    old = [j for j, o in enumerate(prep.symptom_events)
                           if o.status == "active" and o.symptom_code == e.symptom_code
                           and o.occurred_date == e.occurred_date]
                    if old:
                        for j in old:
                            prep.symptom_events[j].status = "superseded"
                        e.supersedes = (prev["report_id"], "symptom", old[0])
                        break

        # duplicates
        dup_of: dict[str, str] = {}
        for k, r in enumerate(patient_rows):
            t = datetime.fromisoformat(r["submitted_at"])
            for prev in patient_rows[:k]:
                dt = (t - datetime.fromisoformat(prev["submitted_at"])).total_seconds()
                if 0 <= dt <= 1800 and _content_key(prev) == _content_key(r):
                    dup_of[r["report_id"]] = prev["report_id"]
                    rep = parsed[r["report_id"]]
                    flags.append(FlagCandidate(
                        flag_type="duplicate_report", severity="info", report_id=r["report_id"],
                        description=f"Same content as {prev['report_id']} submitted {int(dt)}s earlier.",
                        refs=[(r["report_id"], "symptom", i) for i in range(len(rep.symptom_events))]))
                    break

        # conflicts between reports (later report carries the flag)
        for k, r in enumerate(patient_rows):
            rid = r["report_id"]
            for prev in patient_rows[:k]:
                pid = prev["report_id"]
                if dup_of.get(rid) == pid:
                    continue
                for i, e in enumerate(parsed[rid].symptom_events):
                    if e.status != "active":
                        continue
                    for j, o in enumerate(parsed[pid].symptom_events):
                        if o.status != "active":
                            continue
                        why = conflict_reason(o, e)
                        if why:
                            flags.append(FlagCandidate(
                                flag_type="cross_report_conflict", severity="warning", report_id=rid,
                                description=f"{e.symptom_code} on {e.occurred_date}: {pid} vs {rid}: {why}.",
                                refs=[(pid, "symptom", j), (rid, "symptom", i)]))
    return dedupe_flags(flags)


def run(rows: list[dict], ctx: Context, text_parser: TextParser,
        progress: Callable[[int, int], None] | None = None, fallback: bool = True,
        should_stop: Callable[[], str | None] | None = None,
        ) -> tuple[dict[str, ParsedReport], list[FlagCandidate]]:
    """fallback=False is used for evaluation: a failed LLM call is scored as an
    LLM failure, not silently replaced by rule output. should_stop() returning
    a message aborts the run (e.g. wrong model id)."""
    rows = sorted(rows, key=lambda r: (r["submitted_at"], r["report_id"]))
    parsed = {}
    for n, r in enumerate(rows, 1):
        parsed[r["report_id"]] = parse_report(r, ctx, text_parser, fallback)
        if should_stop and (msg := should_stop()):
            raise FatalLLMError(f"aborted after {n}/{len(rows)} reports: {msg}")
        if progress:
            progress(n, len(rows))
    return parsed, cross_report_pass(rows, parsed)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_rows(con: sqlite3.Connection, report_ids: list[str] | None = None) -> list[dict]:
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT report_id, patient_id, submitted_at, source, raw_text, questionnaire_json FROM raw_reports")]
    con.row_factory = None
    if report_ids is not None:
        keep = set(report_ids)
        rows = [r for r in rows if r["report_id"] in keep]
    return rows


def write_results(con: sqlite3.Connection, parsed: dict[str, ParsedReport], cross_flags: list[FlagCandidate],
                  text_method: str, model_name: str | None, prompt_version: str | None = None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    med_id = dict(con.execute("SELECT generic_name, med_id FROM medications"))
    with con:
        con.execute("DELETE FROM flag_events")
        con.execute("DELETE FROM review_flags")
        con.execute("UPDATE symptom_events SET supersedes_event_id = NULL")
        con.execute("DELETE FROM symptom_events")
        con.execute("UPDATE medication_intake_events SET supersedes_intake_id = NULL")
        con.execute("DELETE FROM medication_intake_events")
        con.execute("DELETE FROM weight_measurements WHERE source = 'patient_reported'")
        con.execute("DELETE FROM extraction_runs")

        run_ids = {}
        for method, model, pv, note in [
            ("questionnaire", None, None, "deterministic questionnaire mapping"),
            ("rule", None, None, "keyword baseline" + (" (fallback only)" if text_method == "llm" else "")),
            ("llm", model_name, prompt_version, "Groq structured extraction"),
        ]:
            if method == "llm" and text_method != "llm":
                continue
            run_ids[method] = con.execute(
                "INSERT INTO extraction_runs (method, model_name, prompt_version, run_at, notes) VALUES (?,?,?,?,?)",
                (method, model, pv, now, note)).lastrowid

        ids: dict[tuple, int] = {}
        pending_supersede = []
        for rid, rep in parsed.items():
            for i, e in enumerate(rep.symptom_events):
                cur = con.execute(
                    """INSERT INTO symptom_events (report_id, patient_id, symptom_code, presence, severity_level,
                         severity_nrs, severity_raw, episode_count_min, episode_count_max, occurred_date,
                         occurred_start, part_of_day, time_precision, timing_raw, related_med_id, med_relation,
                         evidence_span, extraction_method, extraction_run_id, confidence, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, rep.patient_id, e.symptom_code, e.presence, e.severity_level, e.severity_nrs,
                     e.severity_raw, e.episode_count_min, e.episode_count_max, e.occurred_date.isoformat(),
                     e.occurred_start, e.part_of_day, e.time_precision, e.timing_raw, med_id.get(e.related_med), e.med_relation,
                     e.evidence_span, e.extraction_method, run_ids.get(e.extraction_method), e.confidence,
                     e.status))
                ids[(rid, "symptom", i)] = cur.lastrowid
                if e.supersedes:
                    pending_supersede.append((cur.lastrowid, e.supersedes))
            for i, e in enumerate(rep.intake_events):
                cur = con.execute(
                    """INSERT INTO medication_intake_events (report_id, patient_id, med_id, med_text_raw,
                         intake_status, dose_count, taken_date, taken_at, part_of_day, time_precision,
                         evidence_span, extraction_method, extraction_run_id, confidence)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, rep.patient_id, med_id.get(e.med), e.med_text_raw, e.intake_status, e.dose_count,
                     e.taken_date.isoformat(), e.taken_at, e.part_of_day, e.time_precision, e.evidence_span,
                     e.extraction_method, run_ids.get(e.extraction_method), e.confidence))
                ids[(rid, "intake", i)] = cur.lastrowid
            for i, w in enumerate(rep.weights):
                cur = con.execute(
                    """INSERT INTO weight_measurements (patient_id, measured_date, weight_kg, source, report_id,
                         evidence_span) VALUES (?,?,?,'patient_reported',?,?)""",
                    (rep.patient_id, w.measured_date.isoformat(), w.weight_kg, rid, w.evidence_span))
                ids[(rid, "weight", i)] = cur.lastrowid
        for new_id, ref in pending_supersede:
            con.execute("UPDATE symptom_events SET supersedes_event_id = ? WHERE event_id = ?", (ids[ref], new_id))

        all_flags = [f for rep in parsed.values() for f in rep.flags] + cross_flags
        flagged_reports = set()
        for f in all_flags:
            pid = parsed[f.report_id].patient_id
            fid = con.execute(
                """INSERT INTO review_flags (patient_id, report_id, flag_type, severity, description, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (pid, f.report_id, f.flag_type, f.severity, f.description, now)).lastrowid
            for ref in f.refs:
                if ref in ids:
                    con.execute("INSERT OR IGNORE INTO flag_events (flag_id, event_table, event_id) VALUES (?,?,?)",
                                (fid, ref[1], ids[ref]))
            if f.severity != "info":
                flagged_reports.add(f.report_id)

        for rid in parsed:
            con.execute("UPDATE raw_reports SET parse_status = ? WHERE report_id = ?",
                        ("needs_review" if rid in flagged_reports else "parsed", rid))
