"""Evaluate the keyword baseline and the Groq LLM parser against blind set 2 (the test) and the two
development sets (gold set, messy set).

Usage:
    .venv\\Scripts\\python.exe evaluate.py --offline  # rule + every LLM prompt version, cache only
    .venv\\Scripts\\python.exe evaluate.py --no-llm   # baseline only

Writes eval/results.md (blind set 2: rule baseline vs the current prompt with and without the flag
layer; main-dataset flag rate; development sets: gold set with every prompt version, messy set; plus
every error) and eval/predictions_<set>_<method>.json (local working files, not committed).

Safety metrics: a report is "wrong" when it is not fully correct (a symptom row missed, spurious or
with a wrong scored field). "Flagged" means the pipeline raised at least one review flag of any type
on that report, so a person sees it. "Wrong & unflagged" is the primary safety number: those errors
reach the chart without anyone looking at them.

Flag layer: the latest prompt is scored twice, with and without the deterministic flag layer
(cinv/flag_layer.py). The layer only adds flags, so extraction metrics are identical; flag rate (share
of reports with any flag) and false-alarm rate (share of fully correct reports that are flagged anyway)
show its cost. Flag precision / recall on the messy sets score every labelled type, including
`other_symptom` and `prompt_injection`, with one rule for all columns; `distress` counts as
`red_flag` and the unlabelled `parser_disagreement` is left out. The gold set has no labels for the
newer types, so it scores only the types in EVAL_FLAG_TYPES.

Matching: within each report, every gold event is paired with the best
predicted event for the same symptom (date > part of day > presence > count).
Unpaired gold events are misses, unpaired predictions are spurious. Field
accuracy is computed on the pairs:
  presence  - all pairs
  count     - vomiting / retching pairs (min AND max must match)
  severity  - nausea / appetite_loss pairs where gold presence != absent
              (severity_level must match exactly, NULL included)
  date      - all pairs (occurred_date)
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from itertools import permutations
from pathlib import Path

from cinv.common import Context
from cinv.llm import (CONTEXT_WINDOW_HOURS, PROMPT_VERSION, PROMPTS, FatalLLMError, GroqExtractor,
                      parse_free_text_llm)
from cinv.models import COUNTED, INFERRED_PRN_TEXT, ParsedReport
from cinv.pipeline import load_rows, run
from cinv.rules import parse_free_text_rule

ROOT = Path(__file__).resolve().parent
EVAL_FLAG_TYPES = {"intra_report_conflict", "cross_report_conflict", "severity_mismatch",
                   "unmapped_medication", "implausible_value", "duplicate_report", "red_flag"}
SEVERITY_SYMPTOMS = {"nausea", "appetite_loss"}
LAYER_SUFFIX = " + flag layer"


def scored_flags(types) -> set[str]:
    """Flag types as scored against labels: distress is handled as a red flag."""
    types = set(types)
    return types | ({"red_flag"} if "distress" in types else set())


class Tally:
    def __init__(self):
        self.c = defaultdict(lambda: [0, 0])     # metric -> [correct, total]

    def add(self, metric: str, ok: bool):
        self.c[metric][0] += int(ok)
        self.c[metric][1] += 1

    def get(self, metric: str) -> tuple[int, int]:
        return tuple(self.c[metric])


def med_source(i) -> str:
    """How the parser knows the drug: inferred from orders, or named by the patient."""
    if i.med_text_raw == INFERRED_PRN_TEXT:
        return "inferred_prn_order"
    return "unmapped" if i.med is None and i.extraction_method == "questionnaire" else "patient_words"


def ev_tuple(e) -> dict:
    if isinstance(e, dict):
        return e
    return {"symptom_code": e.symptom_code, "presence": e.presence, "severity_level": e.severity_level,
            "severity_nrs": e.severity_nrs, "episode_count_min": e.episode_count_min,
            "episode_count_max": e.episode_count_max, "occurred_date": e.occurred_date.isoformat(),
            "part_of_day": e.part_of_day, "related_med": e.related_med, "med_relation": e.med_relation,
            "evidence_span": e.evidence_span}


def short(e: dict) -> str:
    s = f"{e['symptom_code']} {e['presence']} {e['occurred_date']} {e['part_of_day']}"
    if e["symptom_code"] in COUNTED:
        s += f" n={e['episode_count_min']}-{e['episode_count_max']}"
    if e["symptom_code"] in SEVERITY_SYMPTOMS:
        s += f" sev={e['severity_level']}"
    return s


def match(gold: list[dict], pred: list[dict]):
    used, pairs, missed = set(), [], []
    for g in gold:
        best, best_score = None, -1
        for j, p in enumerate(pred):
            if j in used or p["symptom_code"] != g["symptom_code"]:
                continue
            score = (8 * (p["occurred_date"] == g["occurred_date"]) + 4 * (p["part_of_day"] == g["part_of_day"])
                     + 2 * (p["presence"] == g["presence"])
                     + ((p["episode_count_min"], p["episode_count_max"]) ==
                        (g["episode_count_min"], g["episode_count_max"])))
            if score > best_score:
                best, best_score = j, score
        if best is None:
            missed.append(g)
        else:
            used.add(best)
            pairs.append((g, pred[best]))
    spurious = [p for j, p in enumerate(pred) if j not in used]
    return pairs, missed, spurious


def evaluate(gold_reports: list[dict], parsed: dict[str, ParsedReport], cross_flags) -> tuple[dict, list]:
    tallies = {"free_text": Tally(), "questionnaire": Tally(), "all": Tally()}
    errors = []
    cross_by_report = defaultdict(set)
    for f in cross_flags:
        cross_by_report[f.report_id].add(f.flag_type)

    for g in gold_reports:
        rid, exp = g["report_id"], g["expected"]
        rep = parsed[rid]
        t = tallies[g["source"]]
        pred = [ev_tuple(e) for e in rep.symptom_events]
        pairs, missed, spurious = match(exp["symptom_events"], pred)
        errs = []
        for _ in pairs:
            t.add("symptom_recall", True)
            t.add("symptom_precision", True)
        for m in missed:
            t.add("symptom_recall", False)
            errs.append(f"MISSED     {short(m)}")
        for s in spurious:
            t.add("symptom_precision", False)
            errs.append(f"SPURIOUS   {short(s)}  evidence={s['evidence_span']!r}")

        report_ok = not missed and not spurious
        for ge, pe in pairs:
            checks = {"presence": ge["presence"] == pe["presence"],
                      "date": ge["occurred_date"] == pe["occurred_date"],
                      "part_of_day": ge["part_of_day"] == pe["part_of_day"]}
            if ge["symptom_code"] in COUNTED:
                checks["count"] = (ge["episode_count_min"], ge["episode_count_max"]) == \
                                  (pe["episode_count_min"], pe["episode_count_max"])
            if ge["symptom_code"] in SEVERITY_SYMPTOMS and ge["presence"] != "absent":
                checks["severity"] = ge["severity_level"] == pe["severity_level"]
            for k, ok in checks.items():
                t.add(k, ok)
            bad = [k for k, ok in checks.items() if not ok]
            if bad:
                report_ok = False
                errs.append(f"WRONG      {ge['symptom_code']}: " + ", ".join(
                    f"{k} expected {_field(ge, k)} got {_field(pe, k)}" for k in bad)
                    + f"  evidence={pe['evidence_span']!r}")

        # intake rows: (med, status, date, med_source) - an inferred drug name must be marked as inferred
        gi = [(i["med"], i["intake_status"], i["taken_date"], i.get("med_source", "patient_words"))
              for i in exp["intake_events"]]
        pi = [(i.med, i.intake_status, i.taken_date.isoformat(), med_source(i)) for i in rep.intake_events]
        rest = list(pi)
        for x in gi:
            hit = x in rest
            t.add("intake_recall", hit)
            if hit:
                rest.remove(x)
            else:
                errs.append(f"INTAKE MISSED {x}")
        for x in rest:
            t.add("intake_precision", False)
            errs.append(f"INTAKE SPURIOUS {x}")
        for _ in range(len(pi) - len(rest)):
            t.add("intake_precision", True)

        # flags (report-level + cross-report, restricted to gold-labelled types)
        gold_flags = set(exp["flags"]) | set(exp["cross_report_flags"])
        pred_flags = scored_flags({f.flag_type for f in rep.flags} | cross_by_report[rid]) & EVAL_FLAG_TYPES
        for f in gold_flags:
            t.add("flag_recall", f in pred_flags)
            if f not in pred_flags:
                errs.append(f"FLAG MISSED {f}")
        for f in pred_flags:
            t.add("flag_precision", f in gold_flags)
            if f not in gold_flags:
                errs.append(f"FLAG SPURIOUS {f}")

        if exp["supersedes_report_id"]:
            ok = any(e.supersedes and e.supersedes[0] == exp["supersedes_report_id"] for e in rep.symptom_events)
            t.add("correction_linked", ok)
            if not ok:
                errs.append(f"CORRECTION not linked to {exp['supersedes_report_id']}")

        t.add(f"report_exact[{g['label_source']}]", report_ok)
        flagged = bool(rep.flags) or bool(cross_by_report[rid])
        t.add("wrong_flagged", not report_ok and flagged)
        t.add("wrong_unflagged", not report_ok and not flagged)
        lab = g["label_source"]
        tallies["all"].add(f"flag_rate[{lab}]", flagged)
        if report_ok:
            tallies["all"].add(f"false_alarm[{lab}]", flagged)
        if rep.dropped:
            errs += [f"DROPPED    {d}" for d in rep.dropped]
        if rep.failed:
            errs.append("FAILED     parser returned no usable output")
        if any(f.flag_type == "low_confidence" and "fallback" in f.description for f in rep.flags):
            errs.append("FALLBACK   LLM failed for this report; rule parser output was used")
        if errs:
            errors.append({"report_id": rid, "source": g["source"], "case_tags": g["case_tags"],
                           "input": g["input"], "errors": errs})
    return tallies, errors


def _field(e: dict, k: str):
    if k == "date":
        return e["occurred_date"]
    if k == "count":
        return f"{e['episode_count_min']}-{e['episode_count_max']}"
    if k == "severity":
        return e["severity_level"]
    return e[k]


# ---------------------------------------------------------------------------
# Labelled messy sets (labels drafted by AI and reviewed by the author before any parser ran):
#   blind set 2  data/blind_set2.json + data/blind2_review.csv     fresh blind test of the frozen flag layer
#   messy set    data/messy_blind_set.json + data/blind_review.csv development set (flag layer designed on it)
# ---------------------------------------------------------------------------

# Predicted flag types left out of flag precision / recall: parser_disagreement is a second-opinion signal that
# is never labelled, and distress is scored as red_flag (see scored_flags). Every other type, including
# other_symptom and prompt_injection, is scored against the labels, in every column.
UNSCORED_FLAG_TYPES = {"parser_disagreement", "distress"}
BLIND2_SET = ROOT / "data" / "blind_set2.json"
BLIND2_LABELS = ROOT / "data" / "blind2_review.csv"
BLIND_SET = ROOT / "data" / "messy_blind_set.json"
BLIND_LABELS = ROOT / "data" / "blind_review.csv"
# Dates are only labelled (and scored) where resolving the date is the point of the category.
BLIND_DATE_CATEGORIES = {"multi_day_catch_up", "cycle_day_references", "time_ambiguity"}


def _parse_label_symptoms(text: str) -> list[dict]:
    """'vomiting, present, 2–null times' -> dict; '(no symptom events)' -> []. A row without a
    count part has no labelled count (count not scored)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("("):
            continue
        parts = [x.strip() for x in line.split(",")]
        row = {"symptom_code": parts[0], "presence": parts[1], "count": None}
        if len(parts) > 2:
            lo, hi = parts[2].replace(" times", "").split("–")
            row["count"] = (None if lo == "null" else int(lo), None if hi == "null" else int(hi))
        out.append(row)
    return out


def load_blind(set_path: Path = BLIND2_SET, labels_path: Path = BLIND2_LABELS) -> list[dict]:
    """Messy-set reports joined with their reviewed labels. Set 1 has no `kind` (all edge cases)."""
    texts = {r["report_id"]: r for r in json.loads(set_path.read_text(encoding="utf-8"))["reports"]}
    out = []
    with labels_path.open(encoding="utf-8-sig", newline="") as f:
        for lab in csv.DictReader(f):
            rep = texts[lab["report_id"]]
            sym = _parse_label_symptoms(lab["expected_symptoms"])
            dates = lab.get("expected_date", "").splitlines()
            score_dates = rep["category"] in BLIND_DATE_CATEGORIES
            assert not score_dates or len(dates) == len(sym), lab["report_id"]
            for i, row in enumerate(sym):
                d = dates[i].strip() if score_dates else "any"
                row["dates"] = None if d == "any" else set(d.split("|"))
            flags = set() if lab["expected_flags"].strip() == "none" else \
                {x.strip() for x in lab["expected_flags"].split(";")}
            out.append({"report_id": lab["report_id"], "category": rep["category"],
                        "kind": rep.get("kind", "edge"),
                        "submitted_at": rep["submitted_at"], "text": rep["text"],
                        "symptoms": sym, "flags": flags})
    return out


def _blind_pair_checks(g: dict, p: dict) -> dict[str, bool]:
    checks = {"presence": g["presence"] == p["presence"]}
    if g["count"] is not None and g["symptom_code"] in COUNTED:
        checks["count"] = g["count"] == (p["episode_count_min"], p["episode_count_max"])
    if g["dates"] is not None:
        checks["date"] = p["occurred_date"] in g["dates"]
    return checks


def _blind_match(gold: list[dict], pred: list[dict]):
    """Best one-to-one assignment (at most a few rows, so brute force): most pairs, then most
    passing checks."""
    slots = list(range(len(pred))) + [None] * len(gold)
    best, best_score = (None,) * len(gold), -1
    for perm in set(permutations(slots, len(gold))):
        score = 0
        for g, j in zip(gold, perm):
            if j is None:
                continue
            if pred[j]["symptom_code"] != g["symptom_code"]:
                score = -1
                break
            score += 10 + sum(_blind_pair_checks(g, pred[j]).values())
        if score > best_score:
            best, best_score = perm, score
    pairs = [(g, pred[j]) for g, j in zip(gold, best) if j is not None]
    missed = [g for g, j in zip(gold, best) if j is None]
    used = {j for j in best if j is not None}
    return pairs, missed, [p for j, p in enumerate(pred) if j not in used]


def _blind_short(g: dict) -> str:
    s = f"{g['symptom_code']} {g['presence']}"
    if g["count"]:
        s += f" n={g['count'][0]}-{g['count'][1]}"
    return s


def evaluate_blind(blind: list[dict], parsed: dict[str, ParsedReport]) -> tuple[dict, list[dict]]:
    """Overall and per-category tallies, plus one record per report."""
    by_cat: dict[str, Tally] = defaultdict(Tally)
    overall = Tally()
    records = []
    for b in blind:
        rep = parsed[b["report_id"]]
        pred = [ev_tuple(e) for e in rep.symptom_events]
        pairs, missed, spurious = _blind_match(b["symptoms"], pred)
        errs = [f"MISSED     {_blind_short(m)}" for m in missed]
        errs += [f"SPURIOUS   {short(s)}  evidence={s['evidence_span']!r}" for s in spurious]
        for _ in pairs:
            overall.add("symptom_recall", True)
            overall.add("symptom_precision", True)
        for _ in missed:
            overall.add("symptom_recall", False)
        for _ in spurious:
            overall.add("symptom_precision", False)
        for g, p in pairs:
            checks = _blind_pair_checks(g, p)
            for k, ok in checks.items():
                overall.add(k, ok)
            bad = [k for k, ok in checks.items() if not ok]
            if bad:
                got = {"presence": p["presence"], "count": f"{p['episode_count_min']}-{p['episode_count_max']}",
                       "date": p["occurred_date"]}
                exp = {"presence": g["presence"],
                       "count": f"{g['count'][0]}-{g['count'][1]}" if g["count"] else None,
                       "date": "|".join(sorted(g["dates"] or []))}
                errs.append(f"WRONG      {g['symptom_code']}: " + ", ".join(
                    f"{k} expected {exp[k]} got {got[k]}" for k in bad) + f"  evidence={p['evidence_span']!r}")
        wrong = bool(errs)
        all_flags = {f.flag_type for f in rep.flags}
        flagged = bool(all_flags)
        pred_flags = scored_flags(all_flags) - UNSCORED_FLAG_TYPES
        if "red_flag" in b["flags"]:
            overall.add("red_flag_recall", "red_flag" in pred_flags)
        for f in sorted(b["flags"]):
            overall.add("flag_recall", f in pred_flags)
            if f not in pred_flags:
                errs.append(f"FLAG MISSED {f}")
        for f in sorted(pred_flags):
            overall.add("flag_precision", f in b["flags"])
            if f not in b["flags"]:
                errs.append(f"FLAG SPURIOUS {f}")
        if rep.dropped:
            errs += [f"DROPPED    {d}" for d in rep.dropped]
        if rep.failed:
            errs.append("FAILED     parser returned no usable output")
        for t in (overall, by_cat[b["category"]]):
            t.add("report_correct", not wrong)
            t.add("wrong_flagged", wrong and flagged)
            t.add("wrong_unflagged", wrong and not flagged)
            t.add("flag_rate", flagged)
            if not wrong:
                t.add("false_alarm", flagged)
                if not b["flags"]:
                    t.add("false_alarm_unlabelled", flagged)
        overall.add(f"report_correct[{b['kind']}]", not wrong)
        overall.add(f"wrong_unflagged[{b['kind']}]", wrong and not flagged)
        overall.add(f"flag_rate[{b['kind']}]", flagged)
        if not wrong:
            overall.add(f"false_alarm[{b['kind']}]", flagged)
        records.append({"report_id": b["report_id"], "category": b["category"], "input": b["text"],
                        "wrong": wrong, "flagged": flagged, "pred_flags": sorted(all_flags), "errors": errs})
    return {"overall": overall, "by_category": by_cat}, records


BLIND_ROWS = [
    ("Symptom rows - precision", "symptom_precision"),
    ("Symptom rows - recall", "symptom_recall"),
    ("Presence (present/absent/uncertain)", "presence"),
    ("Episode count (min & max, where labelled)", "count"),
    ("Date (3 date categories only)", "date"),
    ("Review flags - precision", "flag_precision"),
    ("Review flags - recall", "flag_recall"),
    ("Red flags - recall (reports labelled red_flag)", "red_flag_recall"),
    ("Report fully correct (symptom rows)", "report_correct"),
    ("Report fully correct - edge cases", "report_correct[edge]"),
    ("Report fully correct - routine", "report_correct[routine]"),
    ("Wrong & flagged (reports)", "wrong_flagged"),
    ("Wrong & unflagged (reports) - lower is better", "wrong_unflagged"),
    ("Flag rate (reports with any flag)", "flag_rate"),
    ("Flag rate - edge cases", "flag_rate[edge]"),
    ("Flag rate - routine", "flag_rate[routine]"),
    ("False-alarm rate (fully correct reports that are flagged)", "false_alarm"),
    ("False-alarm rate, reports with no labelled flag (correct, no flag expected, flagged anyway)",
     "false_alarm_unlabelled"),
    ("False-alarm rate - routine (correct routine reports flagged; no routine report has a labelled flag)",
     "false_alarm[routine]"),
]


def _raw(c: tuple[int, int]) -> str:
    return "-" if c[1] == 0 else f"{c[0]}/{c[1]}"


BLIND2_INTRO = [
    "50 new reports written for this test after the flag layer was frozen (git tag `flag-layer-final`): **35 edge "
    "cases** covering all 22 failure categories of the development messy set, with new personas and scenarios, and "
    "**15 routine reports** to measure false alarms. Text by Groq `qwen/qwen3.8-27b` (`generate_blind_set2.py`), a "
    "different model than the parser's; every report was checked against the development messy set for similarity "
    "(difflib ratio <= 0.6). Labels were drafted by AI and reviewed by the author before any parser ran "
    "(`data/blind2_review.csv`: 47 agreed, 3 revised). Prompt v2, the parsers and the flag rules were not changed "
    "after seeing these results, so every column is a blind number.", "",
    "Severity and medication rows are not labelled; dates are scored only for the 3 date categories "
    "(multi_day_catch_up, cycle_day_references, time_ambiguity). Each report is parsed on its own (no previous "
    "report, no medication orders). \"Wrong\" = at least one symptom row missed, spurious or with a wrong presence / "
    "count / date. Review-flag precision and recall cover every labelled flag type, including `other_symptom` and "
    "`prompt_injection`, with the same rule in all three columns; `distress` counts as `red_flag`; "
    "`parser_disagreement` is never labelled, so it is left out of precision and recall (it still counts towards "
    "flag rate and wrong & unflagged).", "",
]

DEV_MESSY_INTRO = [
    "88 reports (22 failure categories x 4), written by a different model than the parser's, labels drafted by AI and "
    "reviewed by the author before any parser ran (`data/blind_review.csv`).", "",
    "**Development set, not a blind test.** The rule baseline and LLM prompt v2 columns were first scored blind "
    "(prompt v2 is unchanged), but the deterministic flag layer (`cinv/flag_layer.py`) was designed on these 88 "
    "reports after their results had been seen, so its column is in-sample. The blind number for the flag layer is "
    "blind set 2 (top of this file).", "",
    "**Experiment, rejected:** a narrower `parser_disagreement` (only rules-only symptoms, present vs absent, or "
    "non-overlapping counts; LLM-only symptoms ignored). Narrow vs final: wrong & unflagged 7/88 vs 3/88, flag rate "
    "61/88 vs 68/88, false alarms 41/61 vs 44/61, gold tricky flag rate 10/18 vs 11/18, main dataset 31/184 vs "
    "32/184. It let 4 more errors through unflagged (BL04, BL30, BL46, BL48) to save 3 false alarms with almost the "
    "same workload, so the broader (safer) rule is the final, frozen version.", "",
    "Scored exactly like blind set 2. These labels contain no `other_symptom` or `prompt_injection` flags, so such "
    "predicted flags count as spurious here.", "",
]


def blind_markdown(res: dict[str, dict], recs: dict[str, list[dict]], blind: list[dict],
                   intro: list[str]) -> list[str]:
    methods = list(res)
    cats = list(dict.fromkeys(b["category"] for b in blind))
    md = intro + ["| Metric | " + " | ".join(methods) + " |", "|---|" + "---|" * len(methods)]
    for label, key in BLIND_ROWS:
        cells = [fmt(res[m]["overall"].get(key)) for m in methods]
        if not all(c == "-" for c in cells):
            md.append(f"| {label} | " + " | ".join(cells) + " |")
    md += ["", "### By category (raw counts)", "",
           "| Category | n | " + " | ".join(f"{m}: correct | {m}: wrong & unflagged" for m in methods) + " |",
           "|---|---|" + "---|---|" * len(methods)]
    for c in cats:
        cells = [str(sum(b["category"] == c for b in blind))]
        for m in methods:
            t = res[m]["by_category"][c]
            cells += [_raw(t.get("report_correct")), str(t.get("wrong_unflagged")[0])]
        md.append(f"| {c} | " + " | ".join(cells) + " |")
    for m in methods:
        wu = [r for r in recs[m] if r["wrong"] and not r["flagged"]]
        md += ["", f"### Wrong & unflagged - {m} ({len(wu)} reports)", ""]
        for r in wu:
            md += [f"**{r['report_id']}** ({r['category']})  ",
                   f"> {r['input'].replace(chr(10), ' / ') or '(empty message)'}", ""]
            md += [f"- `{x}`" for x in r["errors"]] + [""]
    for m in methods:
        wf = [r for r in recs[m] if r["wrong"] and r["flagged"]]
        md += ["", f"### Wrong & flagged - {m} ({len(wf)} reports)", ""]
        for r in wf:
            md += [f"- **{r['report_id']}** ({r['category']}; flags: {', '.join(r['pred_flags'])}): "
                   + "; ".join(f"`{x}`" for x in r["errors"])]
    last = methods[-1]
    if last.endswith(LAYER_SUFFIX):
        missed = [r for r, b in zip(recs[last], blind)
                  if "red_flag" in b["flags"] and not {"red_flag", "distress"} & set(r["pred_flags"])]
        md += ["", f"### Red flags missed - {last} ({len(missed)} report{'' if len(missed) == 1 else 's'})", ""]
        for r in missed:
            md += [f"- **{r['report_id']}** ({r['category']}): {r['input'].replace(chr(10), ' / ')} "
                   f"(flags raised: {', '.join(r['pred_flags']) or 'none'})"]
    return md


def run_blind(ctx: Context, offline: bool, no_llm: bool, out: Path, set_path: Path, labels_path: Path,
              slug: str, intro: list[str]) -> list[str]:
    blind = load_blind(set_path, labels_path)
    rows = [{"report_id": b["report_id"], "patient_id": f"BLIND-{b['report_id']}",
             "submitted_at": b["submitted_at"], "source": "free_text", "raw_text": b["text"],
             "questionnaire_json": None} for b in blind]
    parsers: dict[str, tuple] = {"Rule baseline": (parse_free_text_rule, None)}
    if not no_llm:
        ex = GroqExtractor(prompt_version=PROMPT_VERSION, offline=offline)
        missing = [r for r in rows if not ex.is_cached(r["raw_text"], r["submitted_at"])]
        if missing and not ex.available:
            print(f"{slug}: LLM {PROMPT_VERSION} skipped ({len(missing)} reports not cached, no API key).")
        else:
            parsers[f"LLM prompt {PROMPT_VERSION}"] = (parse_free_text_llm(ex), ex)
            parsers[f"LLM prompt {PROMPT_VERSION}{LAYER_SUFFIX}"] = (parse_free_text_llm(ex), ex)
    res, recs = {}, {}
    for name, (tp, ex) in parsers.items():
        layer = name.endswith(LAYER_SUFFIX)
        try:
            parsed, _ = run(rows, ctx, tp, fallback=ex is None,
                            should_stop=(lambda: ex.fatal_error) if ex else None, flag_layer=layer)
        except FatalLLMError as e:
            raise SystemExit(f"Evaluation of {slug} stopped, no results written: {e}")
        res[name], recs[name] = evaluate_blind(blind, parsed)
        method = (f"llm_{ex.prompt_version}" + ("_flaglayer" if layer else "")) if ex else "rule"
        (out / f"predictions_{slug}_{method}.json").write_text(json.dumps(
            {rid: json.loads(rep.model_dump_json()) for rid, rep in parsed.items()}, indent=1), encoding="utf-8")
        if ex and not layer:
            print(f"{slug}, {name}: Groq API calls {ex.calls} (rest from cache); "
                  f"reports with no usable LLM output: {sum(r.failed for r in parsed.values())}")
    return blind_markdown(res, recs, blind, intro)


ROWS = [
    ("Symptom detection - precision", "symptom_precision"),
    ("Symptom detection - recall", "symptom_recall"),
    ("Presence (present/absent/uncertain)", "presence"),
    ("Episode count (min & max)", "count"),
    ("Severity level", "severity"),
    ("Date (occurred_date)", "date"),
    ("Part of day", "part_of_day"),
    ("Medication intake (drug, status, date, inferred vs stated) - precision", "intake_precision"),
    ("Medication intake (drug, status, date, inferred vs stated) - recall", "intake_recall"),
    ("Review flags - precision", "flag_precision"),
    ("Review flags - recall", "flag_recall"),
    ("Correction linked to original", "correction_linked"),
    ("Report fully correct - hand-annotated tricky", "report_exact[hand_annotated]"),
    ("Report fully correct - routine", "report_exact[generator]"),
    ("Wrong & flagged (reports; goes to the review queue)", "wrong_flagged"),
    ("Wrong & unflagged (reports; reaches the chart unseen) - lower is better", "wrong_unflagged"),
]


def fmt(c: tuple[int, int]) -> str:
    ok, n = c
    return "-" if n == 0 else f"{ok / n:.0%} ({ok}/{n})"


FLAG_ROWS = [
    ("Flag rate - routine", "flag_rate[generator]"),
    ("Flag rate - tricky", "flag_rate[hand_annotated]"),
    ("False-alarm rate - routine (correct reports flagged)", "false_alarm[generator]"),
    ("False-alarm rate - tricky (correct reports flagged)", "false_alarm[hand_annotated]"),
]


def table(results: dict[str, dict], subset: str, rows=None) -> str:
    methods = list(results)
    head = "| Metric | " + " | ".join(methods) + " |\n|---|" + "---|" * len(methods) + "\n"
    lines = []
    for label, key in rows or ROWS:
        cells = [fmt(results[m][subset].get(key)) for m in methods]
        if all(c == "-" for c in cells):
            continue
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return head + "\n".join(lines)


def main_flag_rate(rows: list[dict], ctx: Context, parsers: dict[str, tuple]) -> list[str]:
    """Flag rate on the whole (unlabelled) dataset: any flag, and warning / urgent only."""
    t: dict[str, Tally] = {}
    for name, (tp, ex) in parsers.items():
        parsed, cross = run(rows, ctx, tp, fallback=ex is None, flag_layer=name.endswith(LAYER_SUFFIX))
        any_f = {rid for rid, rep in parsed.items() if rep.flags} | {f.report_id for f in cross}
        strong = ({rid for rid, rep in parsed.items() if any(f.severity != "info" for f in rep.flags)}
                  | {f.report_id for f in cross if f.severity != "info"})
        t[name] = Tally()
        for rid in parsed:
            t[name].add("any", rid in any_f)
            t[name].add("strong", rid in strong)
    return ["| Metric | " + " | ".join(t) + " |", "|---|" + "---|" * len(t),
            "| Flag rate (reports with any flag) | " + " | ".join(fmt(x.get("any")) for x in t.values()) + " |",
            "| Flag rate, warning or urgent (report set to needs_review) | "
            + " | ".join(fmt(x.get("strong")) for x in t.values()) + " |"]


def f1(t: Tally) -> str:
    p_ok, p_n = t.get("symptom_precision")
    r_ok, r_n = t.get("symptom_recall")
    if not p_n or not r_n or not (p_ok + r_ok):
        return "-"
    p, r = p_ok / p_n, r_ok / r_n
    return f"{2 * p * r / (p + r):.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "cinv.db")
    ap.add_argument("--gold", type=Path, default=ROOT / "data" / "gold_set.json")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--offline", action="store_true", help="never call the API; use cached responses only")
    ap.add_argument("--prompt-versions", nargs="+", default=list(PROMPTS),
                    help="LLM prompt versions to compare (older ones come from the cache)")
    ap.add_argument("--no-blind", action="store_true", help="skip the blind messy set")
    ap.add_argument("--out", type=Path, default=ROOT / "eval")
    args = ap.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))["reports"]
    con = sqlite3.connect(args.db)
    ctx = Context.from_db(con)
    rows = load_rows(con, [g["report_id"] for g in gold])
    all_rows = load_rows(con)
    con.close()

    parsers: dict[str, tuple] = {"Rule baseline": (parse_free_text_rule, None)}
    if not args.no_llm:
        for pv in args.prompt_versions:
            ex = GroqExtractor(prompt_version=pv, offline=args.offline)
            missing = []
            for r in rows:
                if r["source"] == "free_text":
                    text = r["raw_text"]
                else:   # questionnaire comments go to the text parser stripped
                    text = (json.loads(r["questionnaire_json"]).get("comments") or "").strip()
                c = None if pv == "v1" else ctx.previous_report(r["patient_id"], r["submitted_at"],
                                                                CONTEXT_WINDOW_HOURS)
                if text and not ex.is_cached(text, r["submitted_at"], c):
                    missing.append(r["report_id"])
            if missing and not ex.available:
                print(f"LLM {pv} skipped: GROQ_API_KEY not set and {len(missing)} gold reports are not cached.")
                continue
            parsers[f"LLM prompt {pv}"] = (parse_free_text_llm(ex), ex)
            if pv == args.prompt_versions[-1]:
                parsers[f"LLM prompt {pv}{LAYER_SUFFIX}"] = (parse_free_text_llm(ex), ex)

    results, all_errors = {}, {}
    args.out.mkdir(exist_ok=True)
    model = None
    for name, (tp, ex) in parsers.items():
        is_llm = ex is not None
        layer = name.endswith(LAYER_SUFFIX)
        try:
            # no rule fallback for LLM columns: a failed call counts as an LLM failure
            parsed, cross = run(rows, ctx, tp, fallback=not is_llm,
                                should_stop=(lambda: ex.fatal_error) if is_llm else None, flag_layer=layer)
        except FatalLLMError as e:
            raise SystemExit(f"LLM evaluation stopped, no results written: {e}")
        tallies, errors = evaluate(gold, parsed, cross)
        results[name] = {k: v for k, v in tallies.items()}
        all_errors[name] = errors
        slug = (f"llm_{ex.prompt_version}" + ("_flaglayer" if layer else "")) if is_llm else "rule"
        (args.out / f"predictions_{slug}.json").write_text(json.dumps(
            {rid: json.loads(rep.model_dump_json()) for rid, rep in parsed.items()}, indent=1), encoding="utf-8")
        n_failed = sum(rep.failed for rep in parsed.values())
        results[name]["_failed"] = n_failed
        if is_llm and not layer:
            model = ex.model
            print(f"{name}: Groq API calls {ex.calls} (rest from cache); "
                  f"reports with no usable LLM output: {n_failed}")

    n_ft = sum(g["source"] == "free_text" for g in gold)
    n_q = len(gold) - n_ft
    latest = args.prompt_versions[-1] if len(parsers) > 1 else "rule"
    no_blind = ["Not run (--no-blind)."]
    md = ["# Parser evaluation", "",
          "Raw counts are shown next to every percentage. Samples are small (one report moves a metric by "
          "several points), so no statistical significance is claimed.", "",
          f"LLM model: {model}, prompt {latest} (frozen). Flag layer: `cinv/flag_layer.py`, frozen at git tag "
          "`flag-layer-final` before blind set 2 was written. All LLM output is read from `data/llm_cache.json`, "
          "so `python evaluate.py --offline` reproduces this file without an API key.", "",
          "Contents: blind set 2 (the test result) and the main-dataset flag rate first; the two development sets "
          "(gold set, messy set) at the end.", "",
          "## Blind set 2 - fresh blind test (50 reports)", ""]
    md += (run_blind(ctx, args.offline, args.no_llm, args.out, BLIND2_SET, BLIND2_LABELS, "blind2", BLIND2_INTRO)
           if not args.no_blind else no_blind) + [""]
    md += [f"## Flag rate - main dataset (all {len(all_rows)} reports; nurse workload)", "",
           "Every report in the synthetic dataset, parsed in submission order with cross-report checks "
           "(duplicates, conflicts, corrections). Not labelled, so only the share of reports a nurse has to "
           "open is shown. Older prompt versions are left out.", "",
           *main_flag_rate(all_rows, ctx, {m: p for m, p in parsers.items()
                                           if m == "Rule baseline" or latest in m}), "",
           f"## Development set: gold set ({len(gold)} reports; used to tune the prompt)", "",
           f"{len(gold)} reports ({n_ft} free text, {n_q} questionnaires). Prompt v1 -> v2 changes were made "
           "after looking at errors on this set, so it is a development set, not a test. Columns for older prompt "
           "versions are re-scored from cached responses, so they match their original runs. Questionnaire fields "
           "are parsed by deterministic rules in every column; only their free-text comments differ. Flag "
           "precision / recall here cover the flag types labelled in the gold set (no `low_confidence`, "
           "`other_symptom` or `prompt_injection` labels).", "",
           f"### Free-text reports (n={n_ft})", "", table(results, "free_text"), "",
           "Symptom detection F1: " + ", ".join(f"{m} {f1(results[m]['free_text'])}" for m in results), "",
           "Reports where the parser produced no usable output (scored as all-missed): "
           + ", ".join(f"{m} {results[m]['_failed']}" for m in results), "",
           f"### Questionnaire reports (n={n_q})", "", table(results, "questionnaire"), "",
           f"### Flag rate and false alarms - gold set (all {len(gold)} reports)", "",
           "Flag rate = reports with at least one flag of any type (including info). False-alarm rate = fully "
           "correct reports that are flagged anyway. Routine = generator-labelled, tricky = hand-annotated.", "",
           table(results, "all", FLAG_ROWS), ""]
    for name, errors in all_errors.items():
        if name.endswith(LAYER_SUFFIX):     # same rows as without the layer; only flags differ
            continue
        md += [f"### Gold set errors - {name} ({len(errors)} reports with at least one error)", ""]
        for e in errors:
            inp = e["input"] if isinstance(e["input"], str) else json.dumps(e["input"])
            md += [f"**{e['report_id']}** ({e['source']}; {', '.join(e['case_tags'])})  ", f"> {inp}", ""]
            md += [f"- `{x}`" for x in e["errors"]] + [""]
    md += ["## Development set: messy set (88 reports; used to design the flag layer)", ""]
    md += (run_blind(ctx, args.offline, args.no_llm, args.out, BLIND_SET, BLIND_LABELS, "blind", DEV_MESSY_INTRO)
           if not args.no_blind else no_blind) + [""]
    out = args.out / "results.md"
    out.write_text("\n".join(md), encoding="utf-8")

    print(f"\nFREE TEXT (n={n_ft})\n" + table(results, "free_text"))
    print(f"\nQUESTIONNAIRE (n={n_q})\n" + table(results, "questionnaire"))
    print("\nFLAG RATE / FALSE ALARMS (gold)\n" + table(results, "all", FLAG_ROWS))
    for name, errors in all_errors.items():
        if name == f"LLM prompt {latest}":
            print(f"\n{name} errors ({len(errors)} reports):")
            for e in errors:
                print(f"  {e['report_id']} [{', '.join(e['case_tags'])}]")
                for x in e["errors"]:
                    print(f"     {x}")
    print(f"\nFull report: {out}")


if __name__ == "__main__":
    main()
