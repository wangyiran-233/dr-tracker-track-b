"""Evaluate the keyword baseline and the Groq LLM parser against the gold set.

Usage:
    .venv\\Scripts\\python.exe evaluate.py            # rule + LLM prompt v1 + v2 (key or cache)
    .venv\\Scripts\\python.exe evaluate.py --no-llm   # baseline only

Writes eval/results_<latest prompt>.md (tables + every error) and eval/predictions_<method>.json.

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
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from cinv.common import Context
from cinv.llm import (CONTEXT_WINDOW_HOURS, LATEST_PROMPT_VERSION, FatalLLMError, GroqExtractor,
                      parse_free_text_llm)
from cinv.models import COUNTED, INFERRED_PRN_TEXT, ParsedReport
from cinv.pipeline import load_rows, run
from cinv.rules import parse_free_text_rule

ROOT = Path(__file__).resolve().parent
EVAL_FLAG_TYPES = {"intra_report_conflict", "cross_report_conflict", "severity_mismatch",
                   "unmapped_medication", "implausible_value", "duplicate_report", "red_flag"}
SEVERITY_SYMPTOMS = {"nausea", "appetite_loss"}


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
    tallies = {"free_text": Tally(), "questionnaire": Tally()}
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
        pred_flags = ({f.flag_type for f in rep.flags} | cross_by_report[rid]) & EVAL_FLAG_TYPES
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
]


def fmt(c: tuple[int, int]) -> str:
    ok, n = c
    return "-" if n == 0 else f"{ok / n:.0%} ({ok}/{n})"


def table(results: dict[str, dict], subset: str) -> str:
    methods = list(results)
    head = "| Metric | " + " | ".join(methods) + " |\n|---|" + "---|" * len(methods) + "\n"
    lines = []
    for label, key in ROWS:
        cells = [fmt(results[m][subset].get(key)) for m in methods]
        if all(c == "-" for c in cells):
            continue
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return head + "\n".join(lines)


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
    ap.add_argument("--prompt-versions", nargs="+", default=["v1", LATEST_PROMPT_VERSION],
                    help="LLM prompt versions to compare (older ones come from the cache)")
    ap.add_argument("--out", type=Path, default=ROOT / "eval")
    args = ap.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))["reports"]
    con = sqlite3.connect(args.db)
    ctx = Context.from_db(con)
    rows = load_rows(con, [g["report_id"] for g in gold])
    con.close()

    parsers: dict[str, tuple] = {"Rule baseline": (parse_free_text_rule, None)}
    if not args.no_llm:
        for pv in args.prompt_versions:
            ex = GroqExtractor(prompt_version=pv)
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

    results, all_errors = {}, {}
    args.out.mkdir(exist_ok=True)
    model = None
    for name, (tp, ex) in parsers.items():
        is_llm = ex is not None
        try:
            # no rule fallback for LLM columns: a failed call counts as an LLM failure
            parsed, cross = run(rows, ctx, tp, fallback=not is_llm,
                                should_stop=(lambda: ex.fatal_error) if is_llm else None)
        except FatalLLMError as e:
            raise SystemExit(f"LLM evaluation stopped, no results written: {e}")
        tallies, errors = evaluate(gold, parsed, cross)
        results[name] = {k: v for k, v in tallies.items()}
        all_errors[name] = errors
        slug = f"llm_{ex.prompt_version}" if is_llm else "rule"
        (args.out / f"predictions_{slug}.json").write_text(json.dumps(
            {rid: json.loads(rep.model_dump_json()) for rid, rep in parsed.items()}, indent=1), encoding="utf-8")
        n_failed = sum(rep.failed for rep in parsed.values())
        results[name]["_failed"] = n_failed
        if is_llm:
            model = ex.model
            print(f"{name}: Groq API calls {ex.calls} (rest from cache); "
                  f"reports with no usable LLM output: {n_failed}")

    n_ft = sum(g["source"] == "free_text" for g in gold)
    n_q = len(gold) - n_ft
    latest = args.prompt_versions[-1] if len(parsers) > 1 else "rule"
    md = [f"# Parser evaluation on the gold set - prompt {latest}", "",
          f"LLM model: {model}. Columns for older prompt versions are re-scored from cached "
          "responses, so they match their original runs.", "",
          f"Gold set: {len(gold)} reports ({n_ft} free text, {n_q} questionnaires). "
          "Questionnaire fields are parsed by deterministic rules in both columns; only their free-text "
          "comments differ.", "",
          f"## Free-text reports (n={n_ft})", "", table(results, "free_text"), "",
          "Symptom detection F1: " + ", ".join(f"{m} {f1(results[m]['free_text'])}" for m in results), "",
          "Reports where the parser produced no usable output (scored as all-missed): "
          + ", ".join(f"{m} {results[m]['_failed']}" for m in results), "",
          f"## Questionnaire reports (n={n_q})", "", table(results, "questionnaire"), ""]
    for name, errors in all_errors.items():
        md += [f"## Errors - {name} ({len(errors)} reports with at least one error)", ""]
        for e in errors:
            inp = e["input"] if isinstance(e["input"], str) else json.dumps(e["input"])
            md += [f"**{e['report_id']}** ({e['source']}; {', '.join(e['case_tags'])})  ", f"> {inp}", ""]
            md += [f"- `{x}`" for x in e["errors"]] + [""]
    out = args.out / f"results_{latest}.md"
    out.write_text("\n".join(md), encoding="utf-8")

    print(f"\nFREE TEXT (n={n_ft})\n" + table(results, "free_text"))
    print(f"\nQUESTIONNAIRE (n={n_q})\n" + table(results, "questionnaire"))
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
