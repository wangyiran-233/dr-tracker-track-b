"""Parse all raw reports into the structured tables and fill review_flags.

Usage:
    .venv\\Scripts\\python.exe parse_reports.py --text-parser llm     # Groq (needs GROQ_API_KEY in .env)
    .venv\\Scripts\\python.exe parse_reports.py --text-parser rule    # keyword baseline, no API

Re-runnable: derived tables are cleared and rebuilt each time; raw_reports
are never modified (except parse_status). LLM responses are cached in
data/llm_cache.json, so a re-run only calls the API for new reports.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from cinv.common import Context
from cinv.llm import FatalLLMError, GroqExtractor, parse_free_text_llm
from cinv.pipeline import load_rows, run, write_results
from cinv.rules import parse_free_text_rule

DB = Path(__file__).resolve().parent / "data" / "cinv.db"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text-parser", choices=["llm", "rule"], required=True)
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys = ON")
    ctx = Context.from_db(con)
    rows = load_rows(con)

    model, prompt_version = None, None
    if args.text_parser == "llm":
        ex = GroqExtractor()
        if not ex.available:
            n_ft = sum(r["source"] == "free_text" for r in rows)
            print(f"GROQ_API_KEY not set: only cached responses can be used; uncached free-text "
                  f"reports ({n_ft} total) will fall back to the keyword rules and be flagged.", file=sys.stderr)
        text_parser, model, prompt_version = parse_free_text_llm(ex), ex.model, ex.prompt_version
    else:
        text_parser = parse_free_text_rule

    def progress(n, total):
        if n % 20 == 0 or n == total:
            print(f"  parsed {n}/{total}", flush=True)

    try:
        parsed, cross = run(rows, ctx, text_parser, progress,
                            should_stop=(lambda: ex.fatal_error) if args.text_parser == "llm" else None)
    except FatalLLMError as e:
        con.close()
        raise SystemExit(f"Stopped, database left unchanged: {e}")
    write_results(con, parsed, cross, args.text_parser, model, prompt_version)

    if args.text_parser == "llm":
        print(f"Groq API calls this run: {ex.calls} (others served from cache)")
    for table in ("symptom_events", "medication_intake_events"):
        print(f"{table:26s} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    n_w = con.execute("SELECT COUNT(*) FROM weight_measurements WHERE source = 'patient_reported'").fetchone()[0]
    print(f"{'patient-reported weights':26s} {n_w}")
    print("review_flags by type:", dict(Counter(t for (t,) in con.execute("SELECT flag_type FROM review_flags"))))
    print("parse_status:", dict(con.execute("SELECT parse_status, COUNT(*) FROM raw_reports GROUP BY 1").fetchall()))
    con.close()


if __name__ == "__main__":
    main()
