"""Create the SQLite database from sql/schema.sql and load the synthetic dataset.

Loads reference tables and raw reports only. symptom_events,
medication_intake_events, patient-reported weights and review_flags are
filled later by the parser.

Usage:
    .venv\\Scripts\\python.exe scripts\\init_db.py            # rebuild data/cinv.db
    .venv\\Scripts\\python.exe scripts\\init_db.py --db other.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "cinv.db"
DEFAULT_SEED = ROOT / "data" / "synthetic_dataset.json"
SCHEMA = ROOT / "sql" / "schema.sql"


def build(db_path: Path, seed_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(seed_path.read_text(encoding="utf-8"))

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    with con:
        con.executescript(SCHEMA.read_text(encoding="utf-8"))

        con.executemany(
            "INSERT INTO medications (generic_name, brand_names, drug_class, is_antiemetic) VALUES (?, ?, ?, ?)",
            [(m["generic_name"], json.dumps(m["brand_names"]), m["drug_class"], m["is_antiemetic"])
             for m in data["medications"]])
        med_id = dict(con.execute("SELECT generic_name, med_id FROM medications"))

        con.executemany(
            """INSERT INTO patients (patient_id, display_name, diagnosis, regimen_name, emetogenic_risk,
                                     enrolled_at, baseline_weight_kg, weight_alert_pct)
               VALUES (:patient_id, :display_name, :diagnosis, :regimen_name, :emetogenic_risk,
                       :enrolled_at, :baseline_weight_kg, :weight_alert_pct)""",
            data["patients"])

        con.executemany(
            """INSERT INTO chemo_administrations (patient_id, cycle_number, administered_at, agents)
               VALUES (:patient_id, :cycle_number, :administered_at, :agents)""",
            data["chemo_administrations"])

        con.executemany(
            """INSERT INTO medication_orders (patient_id, med_id, dose, route, frequency, is_prn,
                                              start_date, end_date, change_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(o["patient_id"], med_id[o["med"]], o["dose"], o["route"], o["frequency"], o["is_prn"],
              o["start_date"], o["end_date"], o["change_reason"]) for o in data["medication_orders"]])

        con.executemany(
            """INSERT INTO weight_measurements (patient_id, measured_date, weight_kg, source)
               VALUES (:patient_id, :measured_date, :weight_kg, :source)""",
            data["weight_measurements"])

        con.executemany(
            """INSERT INTO raw_reports (report_id, patient_id, submitted_at, source, raw_text, questionnaire_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(r["report_id"], r["patient_id"], r["submitted_at"], r["source"], r["raw_text"],
              json.dumps(r["questionnaire"]) if r["questionnaire"] is not None else None)
             for r in data["raw_reports"]])
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    args = ap.parse_args()
    build(args.db, args.seed)

    con = sqlite3.connect(args.db)
    for table in ("patients", "medications", "chemo_administrations", "medication_orders",
                  "weight_measurements", "raw_reports"):
        print(f"{table:24s} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
    con.close()
    print(f"database: {args.db}")


if __name__ == "__main__":
    main()
