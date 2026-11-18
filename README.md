# CINV patient-reported data: store, manage and chart

Track B prototype for the Dr.Tracker data-intelligence assessment. Patients on chemotherapy report nausea and
vomiting (CINV) in their own words or through a short questionnaire. The pipeline turns each report into structured
data that can be traced back to the original text, and a Streamlit dashboard shows a provider what needs attention
in about 30 seconds.

> **Synthetic data only — not for clinical use.** All patients are fictional, and nothing here is a diagnosis, a
> clinical grade or clinical advice.

![Overview page](docs/screenshots/overview.png)

## Run it

Requires **Python 3.11 or newer** (built on 3.13). **No API key is needed**: the dashboard reads the committed,
pre-built `data/cinv_demo.db`, and the launcher never calls the API. The first run creates `.venv`, installs
`requirements.txt` and opens the dashboard at http://localhost:8501.

**Windows (PowerShell)**

```powershell
git clone https://github.com/wangyiran-233/dr-tracker-track-b.git; cd dr-tracker-track-b
powershell -ExecutionPolicy Bypass -File .\run_demo.ps1
```

**macOS / Linux (Terminal)**

```bash
git clone https://github.com/wangyiran-233/dr-tracker-track-b.git && cd dr-tracker-track-b
bash run_demo.sh        # on macOS, install Python first if needed, e.g. `brew install python@3.13`
```

<details><summary>Tests and full rebuild (also offline)</summary>

```bash
python -m pip install -r requirements-dev.txt && python -m pytest       # inside .venv; no network
python generate_data.py && python init_db.py --db data/cinv_demo.db
python parse_reports.py --text-parser llm --offline --strict --db data/cinv_demo.db   # from data/llm_cache.json
python evaluate.py --offline                                            # writes eval/results.md
```
</details>

## What is in the repository

| Path | Contents |
|---|---|
| `schema.sql`, `queries.sql`, `run_queries.py` | SQLite schema (immutable raw reports, event tables, review flags, views) and 4 provider queries |
| `cinv/`, `parse_reports.py` | Questionnaire parser, keyword-rule baseline, Groq LLM parser, validation and the write pipeline |
| `app.py`, `dashboard/` | Streamlit dashboard: Overview and Patient Deep-Dive, plus a developer inspector behind a sidebar toggle |
| `generate_data.py`, `init_db.py`, `data/` | Synthetic dataset (5 patients × 6 weeks, 184 reports), gold set, blind messy set, LLM cache, demo DB |
| `evaluate.py`, `eval/results.md` | Rule vs LLM evaluation with raw counts and every error |
| `tests/`, `docs/` | pytest suite; [design decisions](docs/design_decisions.md) and screenshots |

## Key design decisions

- **Raw reports are immutable.** Triggers block edits; every structured row links to its report and the exact
  `evidence_span` it came from, so parsers can be re-run and audited.
- **Event (long) model.** One report → many symptom, medication and weight events; a new symptom adds rows, not columns.
- **Unknown stays NULL.** Vague counts become ranges ("a few" = 2–4) and vague times carry a precision. "Reported 0"
  and "not reported" are kept apart all the way to the chart.
- **Conflicts are kept and flagged, not silently resolved.** Both versions are stored; the chart view applies one
  documented rule (take the larger count and mark the day).
- **The LLM reads language; code does everything checkable**: dates, drug mapping, plausibility, every review flag.
  Every `evidence_span` must appear verbatim in the report.

## Results at a glance

**Gold set** — 30 reports (23 free text, 7 questionnaires), labels human-reviewed. Free-text results, raw counts,
no significance claimed (full tables and errors: [`eval/results.md`](eval/results.md)):

| Free text (n = 23) | Rule baseline | LLM prompt v2 |
|---|---|---|
| Presence / episode count / severity | 38/39 · 14/15 · 17/18 | 39/39 · 15/15 · 18/18 |
| Part of day | 35/39 | 36/39 |
| Review-flag recall · correction linked | 4/5 · 0/1 | 5/5 · 1/1 |
| Report fully correct — tricky / routine | 11/16 · 7/7 | 13/16 · 5/7 |
| Wrong & unflagged · wrong & flagged (reports) | 3/23 · 2/23 | 3/23 · 2/23 |

Questionnaires: 100% in every field (deterministic).

**Blind messy set** — 88 reports (22 failure categories × 4) written by a different model, labels human-reviewed
before any parser ran; the prompt and rules were not changed after seeing the results. Each report is parsed on its
own; dates are scored only for the 3 date categories, severity and medication rows are not labelled:

| Blind set (n = 88) | Rule baseline | LLM prompt v2 |
|---|---|---|
| Symptom rows recall · precision | 75/124 · 75/78 | 111/124 · 111/114 |
| Presence · count · date (date categories) | 66/75 · 29/38 · 6/14 | 102/111 · 46/52 · 13/17 |
| Report fully correct | 35/88 | 61/88 |
| **Wrong & unflagged** · wrong & flagged | **50/88** · 3/88 | **24/88** · 3/88 |
| Review-flag recall · red-flag recall | 4/61 · 2/9 | 11/61 · 4/9 |

The LLM is clearly better, but its errors are almost never flagged: 24 of its 27 wrong reports reach the chart
unseen. Most are hedged or second-hand wording turned into a confident row (or the reverse), missed implied
nausea, and wrong dates for cycle-day and catch-up reports. Review flags are mostly missed (11/61, red flags 4/9):
there is no deterministic flag for uncertain presence, distress, non-English text or out-of-schema complaints.
Per-category counts and every error: [`eval/results.md`](eval/results.md#blind-messy-set).

## Where AI was used and how it was checked

| Use | How it was checked |
|---|---|
| Free-text extraction (Groq `openai/gpt-oss-120b`, in the product) | Gold-set evaluation, schema validation, verbatim evidence check; dates, drugs and flags stay deterministic |
| Coding assistant (Claude) for code, SQL, generator and docs | Everything run end to end, pytest suite, dashboard rendered and inspected; design choices set by the author |
| Gold-label drafts for the 18 tricky cases (Claude) | Reviewed case by case by the author: 16 agreed, 2 revised (`data/gold_review.csv`) |
| Blind messy set: text by a different model (Groq `qwen/qwen3.8-27b`), label drafts by Claude | All 88 labels reviewed by the author before any parser ran: 79 agreed, 9 revised (`data/blind_review.csv`) |

## Limitations and next steps

- **Possible prompt and schema improvements:** record clock times the patient states ("at 8", "around 2pm"),
  and ask for the full contiguous evidence phrase, so correct rows are not dropped by the verbatim check.
- **Hybrid routing** (rules for routine reports, LLM for complex ones) and a similarity check against a labelled
  example library are designed but not built ([design decisions](docs/design_decisions.md)).
- The daily max rule can under-count two real episodes reported for the same part of day; LLM confidence is not
  calibrated; red flags are display rules, not triage.
- Questionnaire PRN doses are attributed to the single active PRN order (shown as *inferred*); the questionnaire
  should ask about each ordered drug by name.
