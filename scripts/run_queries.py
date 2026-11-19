"""Run the named queries in sql/queries.sql and print the results.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_queries.py                       # all queries
    .venv\\Scripts\\python.exe scripts\\run_queries.py --name review_queue
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent


def load_queries(path: Path = ROOT / "sql" / "queries.sql") -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"^-- name:\s*(\S+)\s*$", text, flags=re.M)
    return {blocks[i]: blocks[i + 1].strip() for i in range(1, len(blocks), 2)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "cinv.db")
    ap.add_argument("--name")
    args = ap.parse_args()

    queries = load_queries()
    con = sqlite3.connect(args.db)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 70)
    for name, sql in queries.items():
        if args.name and name != args.name:
            continue
        print(f"\n=== {name} ===")
        print(pd.read_sql_query(sql, con).to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
