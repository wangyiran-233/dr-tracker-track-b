# Design decisions

Companion to the [README](../README.md). It covers the architecture, the trade-offs, and what was deliberately left
out. All data is synthetic.

## 1. Problem and goal

Patients on chemotherapy report nausea and vomiting between visits, as free text ("threw up a few times after
lunch") or as a short questionnaire. Before a visit, the provider needs to know who needs attention, how this cycle
compares with the last one, and whether a medication change helped, and they need it in about 30 seconds. The data
also has to stay traceable to what the patient actually wrote. The design goal is provider-first triage on top of
an auditable, conservative data layer.

## 2. Schema

Four principles: **immutable raw reports** (triggers block edits; only `parse_status` changes), **NULL for
unknown**, **conflicts kept and flagged**, and an **event (long) model**. `chemo_administrations` is the time anchor:
every symptom gets a cycle day and a phase. Medication changes close one order and open a new one, so before/after
comparisons stay possible. CTCAE grades are not stored, because they are clinician-assigned.

| Patient says | Stored as |
|---|---|
| "threw up twice" / "a few times" | `episode_count_min/max` = 2/2 / 2/4 |
| "kept throwing up" | 1/NULL (at least one, upper bound unknown) |
| "like a hundred times" | 1/NULL + `implausible_value` flag (code guard, not the LLM) |
| "not sure if it's nausea" | `presence = 'uncertain'`, severity NULL |
| "nausea is worse" | present, `severity_level` NULL (relative word); original words in `severity_raw` |
| "no vomiting today" | `absent`, 0/0: real data, distinct from "not reported" |

| Conflict | Handling |
|---|---|
| Questionnaire 0 vomits, comment "threw up once" | Both rows kept; `intra_report_conflict` |
| Two reports disagree about the same day | Both kept; `cross_report_conflict` |
| "Mild" nausea but cannot keep water down | Stored as said; `severity_mismatch` + `red_flag` |
| "Correction: it was actually 3 times" | Old row `superseded` (not deleted); new row has `supersedes_event_id` |
| Same text twice within a minute | `duplicate_report` |

Daily chart rule: for reports about the same part of day, take the **larger** count and mark the day, because
missing an episode is worse than over-counting. Known cost: two real episodes reported in two separate reports
for the same part of day are under-counted.

**Clinical grounding (inspiration, not validation).** The 0–4 verbal severity bands (none / mild / moderate /
severe / very severe) follow the style of the NCI **PRO-CTCAE** patient-reported items. The 0–10 nausea rating plus
a vomiting-episode count follows the idea behind the **MASCC** Antiemesis Tool. Our questionnaire is neither
instrument and has not been validated. The 0–10 → 0–4 mapping (1–3 / 4–6 / 7–10, with 7–8 vs 9–10 as a project
convention) is our own. CINV phases use the common definitions **acute = 0–24 h** and **delayed = 24–120 h** after
chemotherapy, applied at day granularity (cycle day 1 vs days 2–5). The weight alert line uses
`patients.weight_alert_pct` (default 5%), a **practice-configurable threshold** per patient, not a clinical
recommendation.

## 3. Parser: what the LLM does vs. what code does

| LLM (Groq `openai/gpt-oss-120b`, JSON mode, temperature 0) | Deterministic code |
|---|---|
| Which symptoms; present / absent / uncertain | Dates (the LLM returns only a relative day offset) |
| Counts, severity words, time words | Mapping medication mentions to the dictionary (no guessed drug names) |
| Medication *mentions* in the patient's words; "is this a correction?" | Counting conventions, plausibility guard, every review flag and correction link |

Questionnaires are parsed deterministically. PRN dose counts are attributed to the single active PRN order and
marked *inferred*; with several active PRN orders, the drug stays NULL and the report is flagged. The keyword-rule
parser is both the baseline and the per-report fallback.

## 4. Validation and evaluation

Validation layers: Pydantic schema (one retry with the error fed back); every row's `evidence_span` must appear
**verbatim** in the report, or the row is dropped and flagged `low_confidence`; self-reported confidence < 0.6 is
flagged (a weak, uncalibrated signal). An auth, quota or model error stops the run with the database unchanged; a
single failed report falls back to rules and is flagged.

**Blind set 2** (50 reports: 35 edge cases over 22 failure categories + 15 routine; text by a different model,
labels drafted by AI and reviewed by the author before any parser ran: 47 agreed, 3 revised) is the test. The flag
layer was designed on an earlier 88-report development set and frozen (tag `flag-layer-final`) before blind set 2
was written; nothing was changed after seeing the results. Raw counts in [`eval/results.md`](../eval/results.md):

| Blind set 2 (n = 50) | Rule baseline | LLM v2 | LLM v2 + flag layer |
|---|---|---|---|
| Wrong and unflagged | 22/50 (44%) | 14/50 (28%) | **4/50 (8%)** |
| Report fully correct | 27/50 (54%) | 36/50 (72%) | 36/50 (72%) |
| Flag rate | 2/50 (4%) | 5/50 (10%) | 30/50 (60%) |
| False-alarm rate (correct reports flagged) | 1/27 (4%) | 5/36 (14%) | 20/36 (56%) |
| Red-flag recall | 0/5 (0%) | 2/5 (40%) | 4/5 (80%) |
| False alarms on routine reports | 0/13 (0%) | 0/13 (0%) | 1/13 (8%) |

Flag precision and recall score every labelled flag type, including `other_symptom` and `prompt_injection`, with
one rule for all three columns (`distress` counts as `red_flag`; the unlabelled `parser_disagreement` is left out).

**Deterministic flag layer (implemented).** The development set showed that flags, not extraction, were the weak
point, so `cinv/flag_layer.py` runs after the LLM with the prompt unchanged. It only adds flags: uncertain presence,
hedging, empty / emoji-only / non-English text, second-hand accounts and uncertain dates → `low_confidence`; any
rules-vs-LLM disagreement on symptoms, presence or counts → `parser_disagreement`; distress → `distress` (urgent,
shown as a red flag); out-of-schema complaints → `other_symptom` (fever also `red_flag`); injection-like text →
`prompt_injection`. **The cost is review load**: flag rate 60% (30/50) on blind set 2, which is mostly messy text;
17% on the 184-report everyday main dataset (27 → 32 reports with any flag); 1 of 13 correct routine reports in
blind set 2 flagged by mistake. Of the 20 correct blind-set-2 reports it flags, 16 carry a flag in their reviewed
label (e.g. a hedged count). A narrower `parser_disagreement` was tried on the development set and rejected because
it let more errors through unflagged to save a few false alarms.

**Development sets.** The **gold set** (30 reports; 18 tricky labels drafted by AI and reviewed by the author: 16
agreed, 2 revised) was used to tune the prompt (v1 → v2). Both revisions were questionnaire PRN drugs labelled as
patient-stated when they were inferred, which led to the explicit *inferred vs stated* field. The **88-report messy
set** was used to design the flag layer. Their full results are at the end of [`eval/results.md`](../eval/results.md).

**Why "wrong and unflagged" matters more than "wrong and flagged".** A flagged error goes to the review queue. A
nurse sees the patient's words next to the parsed value, so the cost is a minute of review time. An unflagged
error goes straight into the chart and the 7-day summary and looks exactly like a correct value: nobody checks it,
and it can hide a deteriorating patient or invent a problem. The primary safety metric is therefore the
**wrong-and-unflagged count**, reported next to accuracy. A parser with slightly lower accuracy but no silent
errors is preferable. The same principle shapes the guards: the code flags instead of fixing (exaggerated counts,
missing evidence, unmapped drugs).

## 5. Dashboard

Two clinical views are anchored to provider decisions. **Overview**: who needs attention (red flags, weight below
the alert line, silent drop-off of ≥3 days without a report) and a cohort table. **Patient Deep-Dive**: a 4-card
pre-visit header, Timeline vs Cycle-over-Cycle, a regimen-change marker with an observational before/after panel,
and a Nurse Review Flags list (one card per open flag with its evidence and a "Mark Reconciled" button). One
developer page, **Parser & Eval Inspector** (tabs: edge cases + benchmark, browse all reports, review queue,
evaluation, method), sits behind a sidebar toggle that is off by default; which parser produced the data is shown only
there. The edge-case view reads real hard reports from the database and shows the "Schema valid · Evidence verified"
badge only when the stored rows re-load into the Pydantic models and every evidence span is found in the submission. Design choices:

- **Missing ≠ zero**: hatched "NO LOG" = no nausea or vomiting report that day (unknown); green dot at 0 = patient
  reported no vomiting. The table uses the same words.
- **One palette**: teal (#0F766E and its tints) for everything routine, nausea bars included; red only for red flags
  and vomiting, amber only for weight alerts. Nausea (0–4) and vomiting counts share the main chart on two labelled
  axes so a provider reads both at once; PRN doses and weight sit below on the same date axis.
- **Hover-first provenance**: tooltips show the verbatim `evidence_span` and any conflict / uncertainty flag.
- **One time base**: a single **as-of date** (the last day in the dataset) is shown in the subtitle of every page.
  "Days ago", the silent drop-off rule and every "last 7 days" window use it, in SQL (`pre_visit_snapshot`) and in
  the UI alike, so a patient who stopped reporting shows gaps instead of an older, healthier-looking window.

## 6. Trade-offs and deliberate omissions

No clinical grading (CTCAE), no real-time alerts or notifications, English only, no authentication or multi-user
editing (SQLite), and no causal claims from before/after comparisons. Red flags are display-prominence rules, not
triage.

**Emotional distress and crisis messages.** Messages such as "I can't do this anymore", "what's the point" or only
crying emoji contain no CINV symptom. Clinical crisis handling (risk assessment, escalation, contacting the patient)
is **out of the parser's scope**: the parser only extracts structured symptom, medication and weight data, and it
must not try to judge or respond to a crisis. Such a message must still never disappear just because it produces no
symptom rows. The intended handling is to send it to **human review at high priority** (`red_flag`), so a clinician
sees the patient's own words and follows the practice's escalation protocol. The labelled sets mark these reports
this way (`distress_not_symptom`). Both parsers correctly store no symptom rows for them; the deterministic flag
layer raises an urgent `distress` flag (keyword list), which the dashboard shows as a red flag. On blind set 2 the
list missed one: "Honestly not sure I want to keep going with all this. What's the use at my age." (B2_35).

## 7. Next steps (designed, not built)

**Flag-layer word lists and rules, from the blind set 2 misses.** The 4 wrong-and-unflagged reports and the missed
red flag point to concrete gaps (any change must be measured on a new blind set, since blind set 2 has been seen):

| Report | Patient words (excerpt) | Gap |
|---|---|---|
| B2_06 | "throwing up like a handful of times" | vague quantity words (handful, bunch, several) are not hedging cues |
| B2_23 | "stomach is doing gymnastics again. up and down twice. chef's kiss." | figurative / sarcastic wording; both parsers miss it, so they agree and nothing fires |
| B2_26 | "knuckles are killing me … pins and needles … sores in my mouth" | numbness / tingling and mouth sores are missing from the `other_symptom` list |
| B2_45 | "Skipped breakfast this morning." | skipped meals are not read as appetite loss by either parser |
| B2_35 (red flag) | "not sure I want to keep going … What's the use at my age" | distress list has "what's the point" but not "what's the use" / "keep going" |

**Hybrid routing.** On the gold set, rules were 7/7 on routine reports while the LLM did better on tricky ones. A
router would send questionnaires and free text that the rules cover with high confidence (only known template
phrases, no correction or time-shift words, no unmapped drugs, values in range) to the deterministic parser, and
everything else to the LLM. This saves cost and latency and makes routine data fully reproducible. The risk is the
router's own false confidence, so its acceptance rule must be evaluated on a blind set (wrong-and-unflagged among
rule-accepted reports). A cheap variant runs both parsers and flags disagreements.

**Patient-stated clock times.** Store a time when the patient gives one ("took it at 8", "around 2pm"), marked
exact or approximate, and never derive it from the submission time. Today only the part of day is kept.

**Quote the full contiguous evidence phrase.** Some correct rows are dropped by the verbatim check because the
model joins words that are not adjacent in the report. Asking for the whole contiguous phrase (one phrase may support
several rows) would keep those rows without weakening the check.

**Similarity check against a labelled example library.** This would be an extra validation layer. Each extracted
row's evidence span is compared, by embedding similarity, with human-reviewed example sentences (gold + blind
labels). If the nearest examples disagree with the extraction (e.g. the neighbours are labelled *absent* but the
row says *present*), the row is flagged for review. It never overrides the parser. It needs a local embedding model
(no patient text sent out), a calibrated threshold and a growing, reviewed library.

**Platform reuse (design only).** Move the extraction loop, verbatim evidence check, Pydantic retry and cache into a
domain-agnostic core driven by **specialty config packs** (YAML/JSON: valid event codes, units and bounds,
plausibility thresholds, red-flag triggers), e.g. oncology CINV, isotretinoin dermatology, IBD steroid taper.
Generalize `chemo_administrations` to **`care_plan_milestones`** (type, date, protocol step, metadata) so the same
window queries work for infusions, taper steps or lab check-ins. Add an **export adapter** that writes active
events as **HL7 FHIR R4** `Observation` / `QuestionnaireResponse` resources, keeping `evidence_span` provenance, for
Track C EHR delivery. Package `scripts/evaluate.py` (rule vs LLM, evidence verification, wrong-and-unflagged) as a reusable
CLI/CI harness for onboarding new questionnaires.

## 8. Privacy stance

Only synthetic data is used, and only synthetic text was ever sent to the LLM API. **No compliance claim is made.**
Handling real PHI would first require a data-processing / business-associate agreement with the model provider (or
a self-hosted model), data minimisation and de-identification before any model call, access control and audit
logging, encryption at rest and in transit, a retention policy, and a privacy and security review. Clinical
validation would need appropriate clinical governance.
