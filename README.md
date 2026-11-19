# CINV patient-reported data: store, manage and chart

Track B prototype for the Dr.Tracker data-intelligence assessment.

> **Synthetic data only — not for clinical use.** All patients are fictional. Nothing here is a diagnosis, a
> clinical grade or clinical advice.

---

## 1. Project goal

### The problem

Patients on chemotherapy report nausea and vomiting (CINV) between visits, in their own words ("threw up a few
times after lunch") or through a short questionnaire. Providers do not have time to read every message before a
visit.

### The goal

Turn each report into **structured data that can be traced back to the patient's own words**, and show it so a
provider can see **in about 30 seconds who needs attention**, how this cycle compares with the last one, and
whether a medication change helped.

---

## 2. Data design

### Schema

**Core principles**

- **The patient's words are never changed.** `raw_reports` is immutable (database triggers block edits; only
  `parse_status` can change). Parsers can be re-run and audited at any time.
- **Event (long) model.** One report becomes many symptom, medication and weight events. A new symptom adds rows,
  not columns.
- **Unknown stays empty (NULL).** "Not sure if it's nausea" → `presence = 'uncertain'`, severity NULL. "Reported 0"
  and "not reported" stay different all the way to the chart.
- **Counts are ranges.** "Twice" = 2–2, "a few" = 2–4, "kept throwing up" = at least 1, upper bound NULL.
- **Conflicts are kept and flagged, not silently resolved.** Both versions are stored with a review flag. A
  correction marks the old row `superseded` (never deleted) and links the new one to it.
- **Every row is traceable.** Each structured row stores an `evidence_span` that must appear **verbatim** in the
  report, or the row is dropped and flagged.
- **One time anchor.** `chemo_administrations` gives every symptom a cycle day and a phase (acute = day 1,
  delayed = days 2–5).

**Main tables** (`sql/schema.sql`)

| Table | Purpose |
|---|---|
| `patients`, `chemo_administrations` | Who, and when each infusion happened (the time anchor) |
| `medications`, `medication_orders` | Drug dictionary; a change closes one order and opens a new one, so before/after stays comparable |
| `raw_reports` | Immutable patient submissions (free text or questionnaire) |
| `extraction_runs` | Which parser and prompt version produced each set of rows |
| `symptom_events`, `medication_intake_events`, `weight_measurements` | The structured events, each with its `evidence_span` |
| `review_flags`, `flag_events` | Open issues for a nurse (conflict, red flag, low confidence, …) and their history |
| Views `v_daily_symptom_summary`, `v_daily_medication_intake`, `v_weight_trend` | One documented chart rule applied everywhere (active events only; conflicting counts → the larger value, day marked) |

### Parser

Three parsers write to the same tables; every run is recorded in `extraction_runs`.

- **Questionnaire rules.** Questionnaires are parsed fully deterministically.
- **Keyword-rule baseline.** The baseline for evaluation, and the per-report fallback when the LLM output fails
  validation.
- **LLM (prompt v2) + deterministic flag layer.** The **LLM understands the language**; **code does everything
  checkable**.
  - LLM (Groq `openai/gpt-oss-120b`, JSON mode, temperature 0): which symptoms, present / absent / uncertain,
    counts, severity words, time words, medication *mentions*, "is this a correction?".
  - Code: dates (the LLM returns only a relative day offset), mapping drug mentions to the dictionary, the verbatim
    evidence check, the plausibility guard, and **every review flag**.
  - The flag layer (`cinv/flag_layer.py`) runs after the LLM. It never edits the LLM's rows, it only adds flags:
    - `low_confidence`: presence `uncertain`; hedging (maybe, idk, i think, not sure, ??, "like N"); empty,
      emoji-only or non-English text; second-hand accounts (says, told me, husband / mom …); uncertain dates
      (day N, the other day, "or was that yesterday").
    - `parser_disagreement`: the keyword-rule parser and the LLM disagree on symptoms, presence or counts (any
      difference, including a symptom found only by the LLM).
    - `distress` (scared, can't do this, give up, 😭 …): urgent, shown as a red flag.
    - `other_symptom`: complaints outside the schema (fever, diarrhea, constipation, hair loss, fatigue, pain);
      fever also raises a `red_flag`.
    - `prompt_injection`: "ignore previous instructions", "SYSTEM:", "admin mode", JSON-like text.
  - The rules are frozen (git tag `flag-layer-final`). On the everyday main dataset the layer raises the flag rate
    from 15% to **17% (27 → 32 of 184 reports)**.

### Synthetic dataset

Built from the provided CINV case by `scripts/generate_data.py` (fixed seed, fully reproducible):

- **5 fictional head-and-neck cancer patients × 6 weeks**: 12 infusions, 10 medication orders, **184 reports**
  (129 free text, 55 questionnaires).
- A hidden "truth" curve per patient and day follows the commonly described CINV pattern (peak around days 2–3
  after infusion, scaled by the regimen's emetogenic risk). Routine reports are rendered from that truth by
  templates.
- **15 hand-written tricky cases (18 reports)** cover vague counts, conflicts, corrections, a vomited-up dose,
  questionnaire vs. comment disagreement, and more.

### Gold set and blind sets

| Set | Reports | Role |
|---|---|---|
| Gold set (`data/gold_set.json`) | 30 | Development: used to tune the prompt (v1 → v2) |
| Messy set (`data/messy_blind_set.json`) | 88 | Development: used to design the flag layer |
| Blind set 2 (`data/blind_set2.json`) | 50 | **Final test**: nothing was changed after seeing its results |

- **Gold set.** 12 routine reports labelled straight from the generator, plus the 18 tricky reports, whose labels
  AI drafted and the author reviewed one by one (`data/gold_review.csv`).
- **Messy set.** 22 failure categories × 4 (typos, slang, hedging, second-hand accounts, emoji, non-English,
  distress, prompt injection, …), written by a different model than the parser's; labels reviewed by the author
  before any parser ran (`data/blind_review.csv`). The flag layer was designed on it, so its flag-layer numbers are
  in-sample.
- **Blind set 2.** Written after the flag rules were frozen: **35 edge cases** across the same 22 categories and
  **15 routine reports** to measure false alarms. Text by a **different model** (Groq `qwen/qwen3.8-27b`,
  `scripts/generate_blind_set2.py`) with new personas and scenarios, so the parser's model never writes its own test
  set; every report was checked for similarity against the development reports (difflib ratio ≤ 0.6). Labels
  reviewed by the author before any parser ran (`data/blind2_review.csv`).

---

## 3. Streamlit dashboard

Two clinical pages, plus one developer page behind a sidebar toggle (off by default).

### Overview — "Who needs attention today?"

Red flags, weight below the alert line, and silent drop-off (≥ 3 days without a report) at the top; a cohort
table below. Every page uses one **as-of date**, so a patient who stopped reporting shows gaps, not an older,
healthier-looking week.

![Overview](docs/screenshots/overview.png)

### Patient Deep-Dive — "What do I need to know before I walk in?"

- A 4-card pre-visit header.
- Timeline vs. Cycle-over-Cycle chart: nausea (0–4) and vomiting counts on one chart, PRN doses and weight below on
  the same date axis.
- A regimen-change marker with an observational before/after panel (no causal claim).
- **Nurse Review Flags**: one card per open flag, with the patient's words and a "Mark Reconciled" button.
- Missing ≠ zero: hatched "NO LOG" means no report; a green dot at 0 means the patient reported no vomiting.
  Tooltips show the verbatim `evidence_span`.

![Patient Deep-Dive](docs/screenshots/patient_deep_dive.png)

### Developer mode — Parser & Eval Inspector

For the team, not the provider: edge cases with their structured output and a "Schema valid · Evidence verified"
badge (shown only when the stored rows re-load into the Pydantic models and every evidence span is found), all
reports, the review queue, evaluation results and the method.

![Parser & Eval Inspector](docs/screenshots/parser_inspector.png)

---

## 4. AI usage and human-in-the-loop decisions

### Where AI was used and how it was checked

| Where | AI role | How it was checked |
|---|---|---|
| Schema | Claude drafted the tables | Author decided the open questions (symptom scope, conflict rule, medication-intake and chemo-anchor tables, keeping raw 0–10 scores) and reviewed edge cases |
| Free-text parsing | LLM (`openai/gpt-oss-120b`, prompt v2) extracts symptoms, counts, time words, drug mentions | Pydantic schema with retry; verbatim `evidence_span` check; dates and drug mapping in code; flag layer; comparison with the rule parser |
| Synthetic data | Claude wrote `scripts/generate_data.py` | Fixed seed; routine labels come from the hidden truth, not from a model |
| Test text | Qwen (`qwen/qwen3.8-27b`) wrote the messy set and blind set 2 | Different model from the parser's; similarity check against the development reports |
| Labels | Claude drafted labels for tricky and blind reports | Author reviewed every label before any parser ran: gold set 16 agreed / 2 revised, blind set 2 47 agreed / 3 revised |
| Code and dashboard | Claude wrote most of the code | Offline pytest suite; "Schema valid · Evidence verified" badge in the Inspector |

### Key judgments made by the author

- **Inferred vs. stated.** Both gold-set revisions were questionnaire PRN drugs labelled as patient-stated when they
  were inferred, which led to an explicit *inferred vs. stated* field.
- **Misspelled drug names are not auto-matched.** "zofram", "compazene" → flagged `unmapped_medication`, never
  fuzzy-matched to a drug.
- **Implied symptoms are `uncertain`, not `present`.** "zofram helped a bit" → nausea uncertain; "can't keep food
  down" → vomiting uncertain, no count.
- **Self-contradicting reports get `intra_report_conflict`**, not a severity mismatch.
- **Out-of-schema complaints must not disappear silently** (hair loss, fatigue) → flagged for review.
- **A duplicate is flagged only when the earlier report exists**; otherwise the flag is dropped.
- **Distress messages** ("I can't do this anymore") are out of the parser's scope but must go to human review at
  high priority, never vanish because they contain no symptom.
- **Safer over quieter.** A narrower `parser_disagreement` rule was tried on the messy set and rejected: it let 4
  more errors through unflagged to save 3 false alarms.
- **Freeze before testing.** Prompt, parsers and flag rules were frozen before blind set 2 was written; nothing was
  changed after seeing its results.

### Evaluation results on blind set 2

Full tables, every error and the development-set results: [`eval/results.md`](eval/results.md). Raw counts, small
samples, no significance claimed.

| Blind set 2 (n = 50) | Rule baseline | LLM v2 | LLM v2 + flag layer |
|---|---|---|---|
| **Wrong & unflagged** (silent errors) | 22/50 (44%) | 14/50 (28%) | **4/50 (8%)** |
| Report fully correct (symptom rows) | 27/50 (54%) | 36/50 (72%) | 36/50 (72%) |
| **Flag rate** (reports with any flag) | 2/50 (4%) | 5/50 (10%) | **30/50 (60%)** |
| False-alarm rate (correct reports flagged) | 1/27 (4%) | 5/36 (14%) | 20/36 (56%) |
| Red-flag recall (distress counts as red flag) | 0/5 (0%) | 2/5 (40%) | 4/5 (80%) |
| False alarms on routine reports (correct, flagged) | 0/13 (0%) | 0/13 (0%) | 1/13 (8%) |

- "Wrong" = at least one symptom row missed, spurious, or with a wrong presence / count / date.
- Of the 20 correct reports the layer flags, 16 have a flag in their reviewed label (e.g. a hedged count); 4 have
  none.
- Review-flag precision · recall, all labelled types including `other_symptom` and `prompt_injection`:
  2/2 · 2/27 (rules), 5/5 · 5/27 (v2), 15/20 · 15/27 (v2 + flag layer).

**Why "wrong & unflagged" is the main safety metric:** a flagged error goes to a nurse with the patient's words
next to it; an unflagged error looks exactly like a correct value on the chart. Without the layer, all 14 of the
LLM's wrong reports were unflagged; with it, 10 of them are flagged and 4 still reach the chart unseen (B2_06,
B2_23, B2_26, B2_45; see Next steps). The 60% flag rate is the cost: blind set 2 is mostly messy text, and on its
routine reports only 1 of 13 correct reports is flagged by mistake.

---

## 5. Next steps

### Implementation

**Prompt**
- Ask for the **full contiguous evidence phrase**, so correct rows are not dropped by the verbatim check.
- Record **clock times the patient states** ("took it at 8", "around 2pm"), marked exact or approximate.

**System**
- **Improve the flag-layer word lists and rules, guided by what blind set 2 missed** (then measure again on a new
  blind set, since blind set 2 has now been seen):
  - B2_06 "like a handful of times": vague quantity words (handful, bunch, several) are not treated as hedging.
  - B2_23 "stomach is doing gymnastics … up and down twice. chef's kiss": figurative / sarcastic wording; both
    parsers miss the implied vomiting and nausea, so they agree and nothing is flagged.
  - B2_26 "knuckles are killing me … pins and needles … sores in my mouth": numbness / tingling and mouth sores are
    not in the `other_symptom` word list.
  - B2_45 "Skipped breakfast this morning": skipped meals are not read as appetite loss by either parser.
  - Missed red flag B2_35 "Honestly not sure I want to keep going … What's the use at my age": the distress list has
    "what's the point" but not "what's the use" or "not sure I want to keep going".
- **Hybrid routing**: rules for routine reports they cover with high confidence, LLM for the rest; the router's
  acceptance rule is judged by wrong & unflagged (and flag rate) on a blind set.
- **Similarity check against a labelled example library**: compare each evidence span with reviewed examples (local
  embeddings); flag disagreements, never override.
- **PRN questionnaire by drug name** when a patient has several active PRN orders (today the dose is attributed to
  the single active PRN order and shown as *inferred*).

### Project implications

**Track C (EHR delivery)**
- Export active events as **HL7 FHIR R4 `Observation` / `QuestionnaireResponse`** resources.
- Keep `evidence_span` as provenance so the EHR can show the patient's words.
- Generate a **pre-visit summary** from the same views the dashboard uses.

**Other specialties (e.g. dermatology, gastroenterology)**
- One **core engine** (extraction loop, verbatim evidence check, schema retry, cache, evaluation harness).
- One **config pack per specialty**: symptom vocabulary, units and thresholds, red-flag rules.
- Generalize the time anchor from chemotherapy to **care-plan milestones** (infusion, taper step, lab check-in), so
  the same window queries keep working.

**Production readiness**
- SQLite → **Postgres**; **access control and audit logs**.
- Before any real PHI (no compliance claim made here): an agreement with the model provider or a self-hosted model,
  data minimization and de-identification before model calls, encryption at rest and in transit, a retention
  policy, and a privacy and security review.
- **Version models and prompts** (already recorded per run in `extraction_runs`).
- **Run the evaluation in CI**; block changes that raise wrong & unflagged.
- **Monitor after launch**: flag rate and the outcomes of human review.
- **Re-evaluate regularly** on a fresh blind set.

---

## 6. Repository layout

```text
app.py           Streamlit entry point (the dashboard)
run_demo.ps1/.sh one-command demo: create .venv, install, start the dashboard
cinv/            parsing pipeline: rule parser, LLM parser, flag layer, data models
dashboard/       dashboard pages, charts and data access
scripts/         command-line tools: generate data, build the database, parse, evaluate, run queries
sql/             database schema and the named SQL queries used by the dashboard
data/            synthetic dataset, gold and blind sets, review labels, LLM cache, pre-built demo database
eval/            evaluation results (results.md)
tests/           pytest suite (offline, no API key)
docs/            design decisions and dashboard screenshots
```

---

## 7. How to run it

Requires **Python 3.11+** (built on 3.13). **No API key needed**: the dashboard reads the committed, pre-built
`data/cinv_demo.db`, and the LLM output is cached in `data/llm_cache.json`. The first run creates `.venv`, installs
dependencies and opens http://localhost:8501.

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

**Tests and full rebuild** (inside `.venv`, offline)

```bash
python -m pytest
python scripts/generate_data.py && python scripts/init_db.py --db data/cinv_demo.db
python scripts/parse_reports.py --text-parser llm --offline --strict --db data/cinv_demo.db   # from data/llm_cache.json
python scripts/evaluate.py --offline --db data/cinv_demo.db                                   # writes eval/results.md
```

More detail: [docs/design_decisions.md](docs/design_decisions.md).
