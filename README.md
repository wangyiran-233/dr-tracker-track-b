# CINV patient-reported data — store, manage & chart

Prototype for **Track B** of the Dr.Tracker data-intelligence assessment, built on the oncology demo scenario:
**nausea and vomiting during chemotherapy (CINV)**. Patients report in their own words or via a short
questionnaire; the pipeline turns those reports into structured, traceable data and shows a provider what
matters in about 30 seconds.

> **All data is synthetic.** Patients are fictional; regimens and antiemetic orders are simplified for the demo.
> Clinical terms (acute/delayed phase, emetogenic risk, severity bands) follow commonly used definitions **for
> reference only**. Nothing here is a diagnosis, a clinical grade or clinical guidance.
>
> **全部为合成数据。** 临床术语仅"参考常见定义"，不构成诊断或临床建议。

**Stack:** SQLite · Python 3.13 · Pydantic · Groq (`openai/gpt-oss-120b`) · Streamlit + Plotly

---

## Contents

1. [Quick start](#quick-start)
2. [Project layout](#project-layout)
3. [Schema design](#schema-design)
4. [Vague and conflicting reports](#vague-and-conflicting-reports)
5. [Parser design: what the LLM does vs. what code does](#parser-design)
6. [Evaluation: rule vs. LLM v1 vs. v2](#evaluation)
7. [Dashboard](#dashboard)
8. [Where AI was used](#where-ai-was-used)
9. [How the work was verified](#how-the-work-was-verified)
10. [Known limitations and next steps](#known-limitations-and-next-steps)

---

## Quick start

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

.venv\Scripts\python.exe generate_data.py            # synthetic dataset + gold set (deterministic, seed 42)
.venv\Scripts\python.exe init_db.py                  # build data/cinv.db from schema.sql
.venv\Scripts\python.exe parse_reports.py --text-parser rule   # or: --text-parser llm (needs GROQ_API_KEY)
.venv\Scripts\python.exe run_queries.py              # the 4 queries in queries.sql
.venv\Scripts\python.exe evaluate.py                 # rule vs LLM v1 vs v2 on the gold set
.venv\Scripts\python.exe -m streamlit run app.py     # dashboard
```

For the LLM parser, copy `.env.example` to `.env` and set `GROQ_API_KEY`. Optional: `GROQ_MODEL`,
`GROQ_REASONING_EFFORT` (default `medium`), `GROQ_PROMPT_VERSION` (default `v2`).

LLM responses are cached in `data/llm_cache.json` (only schema-valid completions are ever cached), so
`evaluate.py` reproduces the published results **without an API key**.

---

## Project layout

| Path | Purpose |
|---|---|
| `schema.sql` | Tables, triggers and the views the queries and dashboard read |
| `generate_data.py` | 5 synthetic patients × 6 weeks, 184 reports, 15 hand-written tricky cases, gold set |
| `init_db.py` | Builds `data/cinv.db` and loads reference data and raw reports |
| `cinv/models.py` | Pydantic models: the single output contract for every parser |
| `cinv/questionnaire.py` | Deterministic questionnaire parser |
| `cinv/rules.py` | Keyword/regex baseline for free text (also the fallback) |
| `cinv/llm.py` | Groq extractor, prompts v1/v2, validation, cache |
| `cinv/common.py` | Medication dictionary mapping, evidence check, deterministic post-processing |
| `cinv/pipeline.py` | Flags, corrections, cross-report checks, writing to SQLite |
| `parse_reports.py` | Parse every report into the database |
| `evaluate.py` | Field-level evaluation against `data/gold_set.json` |
| `queries.sql`, `run_queries.py` | Four useful queries |
| `app.py`, `dashboard/` | Streamlit dashboard (5 pages) |
| `eval/` | Evaluation results (`results_v1.md` snapshot, `results_v2.md`) and predictions |

---

## Schema design

Four principles drive every table:

1. **Raw reports are immutable.** Patient text is stored once and never edited (enforced by triggers; only
   `parse_status` may change). Every structured row points back to its report *and* to the exact text it was
   based on (`evidence_span`). Parsers can be re-run, compared and audited.
2. **Unknown stays NULL.** Vague amounts become ranges, vague times carry a precision. Nothing is guessed to
   fill a column.
3. **Conflicts are kept, not silently resolved.** Both versions stay in the tables and are flagged; one explicit,
   documented rule is applied only in the view used for charts.
4. **Event (long) model.** One report → many events, so adding a symptom means adding rows, not columns.

| Table | Why it exists |
|---|---|
| `patients` | Synthetic ID, regimen, emetogenic risk of regimen (context for "is this expected?"), baseline weight, provider-configured weight alert % |
| `chemo_administrations` | **The time anchor.** Lets every symptom be placed at "day N of cycle", so cycles can be compared |
| `medications`, `medication_orders` | Dictionary with brand names (parser matching). A change = close the old order + open a new one, never overwrite, so before/after comparisons are possible |
| `raw_reports` | Immutable input. `submitted_at` ≠ when the symptom happened |
| `symptom_events` | Core table: presence, severity (0–4 verbal scale), questionnaire score (0–10, unchanged), episode count **range**, date + part of day + precision, medication relation, evidence, method, confidence, status/supersedes |
| `medication_intake_events` | PRN use is itself a signal; missed / vomited-up doses change how symptoms read |
| `weight_measurements` | Clinic vs patient-reported weights; patient ones link to their report |
| `extraction_runs` | Which parser / model / prompt version produced the rows |
| `review_flags`, `flag_events` | Review queue; the junction table links each flag to the exact events |
| `v_daily_symptom_summary` | One row per patient/day/symptom with the conflict rule, cycle day and phase |
| `v_daily_medication_intake`, `v_weight_trend` | Daily medication use (PRN on that date) and weight vs alert line |

CTCAE grades are deliberately **not** stored: they are clinician-assigned, and auto-grading would be an
unsupported clinical claim.

**中文要点：** 原始报告不可修改，所有结构化数据都能追溯到原文片段；不确定的值存 NULL，不猜；矛盾的数据都保留并标记，
只在图表用的视图里按一条写明的规则取值；化疗给药表是时间锚点，用来计算"化疗后第几天"。

---

## Vague and conflicting reports

### Vague

| Patient says | Stored as |
|---|---|
| "threw up twice" | `episode_count_min/max` = 2/2 |
| "a few times" | 2/4 |
| "kept throwing up" (no number) | 1/NULL — at least one, upper bound unknown |
| "like a hundred times" | 1/NULL **+ `implausible_value` flag** (code guard, not the LLM) |
| "not sure if it's nausea or just tired" | `presence = 'uncertain'`, severity NULL |
| "nausea is worse" | present, `severity_level` NULL (relative word, not a level); original words in `severity_raw` |
| "no vomiting today" | `presence = 'absent'`, 0/0 — **real data, different from "not reported"** |
| "after dinner", "last night" | `part_of_day`, `time_precision`, `timing_raw`; date resolved from `submitted_at` |

### Conflicting

| Situation | Handling |
|---|---|
| Questionnaire says 0 vomits, comment says "threw up once" | Both rows kept; `intra_report_conflict` |
| Two reports disagree about the same day | Both kept; `cross_report_conflict` on the later report |
| "Mild" nausea but cannot keep water down | Stored as said; `severity_mismatch` + `red_flag` |
| "Correction: it was actually 3 times" | Old row → `superseded` (not deleted); new row links via `supersedes_event_id` |
| Same text submitted twice in a minute | `duplicate_report` |

**Daily view rule (documented trade-off):** reports about the *same* part of day are treated as the same
episodes → take the **larger** count (missing a vomiting episode is worse than over-counting) and mark the day;
non-overlapping parts of day are added up; superseded rows are ignored. Known limitation: two genuinely separate
episodes reported in two reports for the same part of day are under-counted.

**中文要点：** 模糊次数存成区间（"几次" = 2–4，"一直吐" = 至少 1 次）；"没吐"是有效数据，和"没报告"严格区分；
矛盾的两条都保留并打 flag，图表里取较大值并标记；更正不删除旧数据，只把旧行标为 superseded。

---

## Parser design

Three parsers, one output contract (`cinv/models.py`):

- **Questionnaire** — deterministic. 0–10 scores stored unchanged; `severity_level` derived with a fixed mapping.
  The free-text comment field goes to the free-text parser, which is how "0 vomits + comment says 1" is caught.
  The PRN question asks for a **dose count, not a drug**: the drug is inferred only when exactly one PRN
  antiemetic order is active that day, and is stored with `med_text_raw = "inferred from active PRN order"` so the
  dashboard shows it as *inferred*, never as the patient's words. With zero or several active PRN orders the drug
  stays NULL and the report gets an `unmapped_medication` flag.
- **Keyword baseline** (`cinv/rules.py`) — clause-level regex. The yardstick the LLM must beat, and the fallback
  when an LLM call fails for one report.
- **Groq LLM** (`cinv/llm.py`) — `openai/gpt-oss-120b`, JSON mode, temperature 0.

### What the LLM does vs. what code does

| LLM (language) | Code (everything checkable) |
|---|---|
| Which symptoms, present/absent/uncertain | Date arithmetic (LLM only returns a relative day offset) |
| Counts, severity words, time words | Mapping medication mentions to the dictionary — **no guessed drug names**; unknown → `unmapped_medication` |
| Medication *mentions* in the patient's words | Counting conventions and plausibility guard |
| Is this a correction? | **Every** review flag, conflict and correction link |

### Validation layers

1. Pydantic schema; on failure one retry with the validation error fed back.
2. **Every row's `evidence_span` must appear verbatim in the report** (case/whitespace/quote-normalised), otherwise
   the row is dropped and the report is flagged `low_confidence`.
3. Self-reported confidence < 0.6 → `low_confidence` flag. Treated as a weak signal: it is not calibrated.
4. Failure handling: wrong model / auth / quota **stops the run and leaves the database unchanged**; a single
   failed report falls back to the keyword rules and is flagged. In evaluation there is no fallback — a failed
   call counts as an LLM failure.

**中文要点：** LLM 只负责"读懂语言"；日期计算、药名映射、计数约定和所有 flag 都由代码完成。
问卷只问按需止吐药吃了几次、不问药名：只有当天恰好一条按需医嘱时才推断药名，并明确标记为"推断"；有 0 条或多条时药名留空并打 flag。
每一行的 evidence 必须能在原文里逐字找到，找不到就丢弃并标记；配置类错误直接停止，不会静默退回规则。

---

## Evaluation

Gold set: **30 reports** (23 free text, 7 questionnaires) — all 18 hand-written tricky reports plus a stratified
sample of 12 routine ones; the 18 tricky labels were human-reviewed (see [Where AI was used](#where-ai-was-used)).
Run `evaluate.py`; full tables and every error are in `eval/results_v2.md` (`eval/results_v1.md` is the v1
snapshot). Medication intake rows are scored on drug, status, date **and** whether the drug was stated by the
patient or inferred from orders.

**Free-text reports (n = 23)**

| Metric | Rule baseline | LLM prompt v1 | LLM prompt v2 |
|---|---|---|---|
| Symptom detection precision / recall | 100% / 98% | 100% / 98% | 100% / 98% |
| Presence | 97% (38/39) | 100% (39/39) | 100% (39/39) |
| Episode count | 93% (14/15) | 100% (15/15) | 100% (15/15) |
| Severity level | 94% (17/18) | 100% (18/18) | 100% (18/18) |
| Date | 100% (39/39) | 100% (39/39) | 97% (38/39) |
| Part of day | 90% (35/39) | 85% (33/39) | 92% (36/39) |
| Review flags recall | 80% (4/5) | 80% (4/5) | 100% (5/5) |
| Correction linked to original | 0/1 | 0/1 | 1/1 |
| Report fully correct — tricky | 69% (11/16) | 75% (12/16) | 81% (13/16) |
| Report fully correct — routine | 100% (7/7) | 86% (6/7) | 71% (5/7) |

Questionnaires are 100% in every column (deterministic fields).

### v1 → v2: each change targets an observed v1 error

1. **Exaggerated counts copied literally.** v1 told the model to normalise "a hundred times" itself, which hid the
   value from the code guard, so `implausible_value` was never raised. → flag recall 80% → 100%.
2. **Part of day only from time words**, never from submission time (v1 labelled "no nausea" as *evening* because
   the report was sent in the evening). → 85% → 92%.
3. **Previous report as context**, so "it was actually 3 times" can be resolved. Extraction stays limited to the
   current report; the verbatim evidence check drops anything copied from context. → correction 0/1 → 1/1.

v2 also introduced two routine errors: **R0043** ("no nausea or vomiting" → evidence `"no vomiting"` is not
contiguous, so the strict check dropped a correct row) and **R0175** ("Appetite is okay" dated today instead of
yesterday — genuinely ambiguous).

### Read these numbers with care / 解读注意事项

- **Tiny sample.** 23 free-text reports: one report moves a metric by several points. No statistical claim.
- **The keyword baseline is optimistic.** Routine reports are rendered from templates with a closed vocabulary,
  and the rules were written after seeing them. Real patient language is far more varied.
- **The fair test is a held-out set** written in varied wording without looking at the rules (next step).
- **中文：** 样本只有 23 条，差一条就差几个百分点，不能下统计结论；关键词基线偏乐观（模板生成的文本 + 规则是看过文本后写的）；
  公平的比较需要一组用词多样、不对照规则写的留出集。

---

## Dashboard

`streamlit run app.py`. Every page states **synthetic data** and **which parser produced the data shown**
(read from `extraction_runs`). Pages 1–2 are designed to be read in about 30 seconds.

1. **Overview** — pre-visit snapshot (query 3), patients with open red flags first, then open review items,
   weight alert, worst nausea. Click a row to open the patient.
2. **Patient trend** — "since last visit" vs the same days of the previous cycle; daily nausea and vomiting
   (vague counts drawn as ranges); grey = no report (unknown) vs dash at 0 = reported none; markers for
   uncertain / intensity-not-stated / needs-review / red flag; infusion and medication-change lines; PRN doses;
   weight with alert line.
3. **Cycle comparison** — cycles overlaid by day of cycle with acute/delayed phases; P002 before/after the
   olanzapine addition (labelled observational, not causal).
4. **Patient's words vs parsed result** — evidence highlighted in the original text, extracted rows and flags
   (resolve / dismiss with a note), review queue with red flags first.
5. **Method & evaluation** — provenance, rule vs v1 vs v2, error examples.

Chart rules: no dual axes (separate panels on a shared date axis), fixed colour roles from a validated palette,
status colours only for red flags / review items and always with a text label.

---

## Where AI was used

| Where | How it was checked | Where it was *not* relied on |
|---|---|---|
| **Free-text extraction** (Groq, in the product) | Gold-set evaluation; verbatim evidence check; schema validation; v1→v2 driven by specific errors | Dates, drug identity, counting conventions, flags, conflict resolution — all deterministic code |
| **Coding assistant** (Claude, for writing code, SQL, the data generator and this README) | Code was run end to end; dashboard pages tested headlessly; charts rendered and inspected; failures fixed and re-run | Design choices were set or approved by the author: track and scenario, schema, conflict policy ("take the larger count and flag"), dashboard page spec |
| **Gold labels for the 18 tricky cases** | **Drafted by Claude, then reviewed case by case by Yiran.** Review record: `data/gold_review.csv` (verdict + comment per case) | Labels were not accepted on the AI's word: 16 agreed, 2 disagreed and were revised (see below). The 12 routine labels come from the generator's hidden truth and were not part of this review |

**Human review of the gold set.** All 18 hand-written cases were reviewed one by one; result **16 agree,
2 disagree**. Both disagreements (**R0035, R0095** — questionnaires) raised the same issue: the PRN drug
(ondansetron) had been labelled as if the patient named it, but it was *inferred* from the single active PRN order.
Changes made: those two labels now carry `med_source = "inferred_prn_order"` (the other 6 intake labels are
`patient_words`); the parser marks inferred drugs, leaves the drug NULL with an `unmapped_medication` flag when
several PRN orders are active, and the dashboard shows "inferred" vs "patient's words"; the evaluation now checks
this distinction. No other label values changed; all metrics were unchanged after re-running from the cache.
Two reviewer comments on agreed cases (R0003, R0067: "the patient gave a clock time — store it") are scheduled as a
next step below.

Privacy: only synthetic data is sent to the API. Real patient data would require appropriate agreements and a
privacy review before any third-party model call; no compliance claim is made here.

**中文要点：** LLM 只用于从自由文本中抽取信息，并通过 gold set、逐字 evidence 检查和 schema 校验来核对；
日期、药名、flag 等都不依赖 LLM。代码由 AI 编程助手协助编写，并经过实际运行和测试。
18 条疑难 case 的标注由 Claude 起草，Yiran 逐条人工复核（记录见 `data/gold_review.csv`）：16 条同意，2 条不同意。
不同意的 R0035、R0095 都是同一个问题：问卷里的按需止吐药药名是根据医嘱推断出来的，却被标得像患者自己说的。
修改内容：这 2 条标注改为 `med_source = inferred_prn_order`；parser 会标记"推断"，有多条按需医嘱时药名留空并打 flag；
看板区分"推断"和"患者原话"；评测也会核对这一区分。其他标注值没有改动，重跑评测（只用缓存）后所有指标不变。
R0003、R0067 的意见（病人给了具体时间，应该存下来）放到"下一步"。

---

## How the work was verified

- **Schema:** all 30 gold labels loaded into a copy of the DB as if they were parser output; every CHECK
  constraint accepted them; immutability triggers fire; the daily view returns the intended values for the
  conflict, correction, duplicate and vague-count cases.
- **Parsers:** gold-set evaluation (above). LLM validation logic tested with a mocked Groq client: schema-failure
  retry, fabricated evidence dropped, exaggerated count flagged, API failure → rule fallback + flag, cache hit
  makes no call.
- **Reproducibility:** `evaluate.py` with no API key reproduces `eval/results_v2.md` exactly from the cache; the
  v1 column matches the saved v1 snapshot line for line.
- **Dashboard:** every page run headlessly (Streamlit AppTest) for every patient and for every flagged report;
  charts exported and visually inspected (fixed clipped ticks, overlapping labels, missing phase shading).

---

## Known limitations and next steps

**Open items**

- **Full LLM parse (prompt v2) is complete** and the dashboard shows it. It took two days: the first run stopped at
  report 163/184 on Groq's free-tier daily token limit (200k tokens/day for this model) with the database left
  unchanged; the second run made the remaining 12 calls (130 LLM reports in total, no rule fallbacks). The
  keyword-rule database is kept locally as `data/cinv_rule.db`.
- **Prompt v3 for R0043 — now the top priority.** On the full dataset the same issue dropped **20 correct rows**:
  every routine report "Feeling pretty good today, no nausea or vomiting." gets evidence `"no vomiting"`, which is
  not contiguous, so the "vomiting: absent" row is rejected (flagged `low_confidence`). Those days appear on the
  dashboard as *no report* instead of *reported none* (123 vs 143 absent-vomiting rows in the rule version).
  Fix: rather than relaxing the evidence check, ask the model to quote the full contiguous phrase
  ("no nausea or vomiting") and allow one span to support several rows. The verification rule stays unchanged. *Considered alternative:* allow evidence assembled from fragments — rescues merged negations, but
  weakens the anti-fabrication guarantee (fragments from different sentences can assemble a meaning the patient
  never stated). If ever adopted: same sentence only, original order, ≤ 2 fragments, lower confidence, distinct
  highlight.
- **Ask for the drug, not just a count (product fix).** Generate the questionnaire's PRN item from the patient's
  active orders — e.g. "How many doses of ondansetron (Zofran) did you take in the last 24 h?", one item per PRN
  order — so the drug is reported by the patient instead of inferred. The prototype currently infers and marks it.
- **Store clock times the patient gives (with v3, from human review of R0003 / R0067).** "Took it at 8 … threw it
  up 20 minutes later", "around 2pm": today only `part_of_day` is kept. The schema already has `occurred_start` /
  `occurred_end` and `time_precision` (`exact` / `approximate`); the prompt and parser need to return a clock time
  when one is stated, with the gold set extended to score it. Rule: only times the patient states or that follow
  arithmetically from them (8:00 + 20 min → ~08:20, `approximate`); never a time inferred from submission time.
- **Held-out evaluation set** in varied wording, written without looking at the rules.
- Move the ad-hoc mock-client and schema checks (including the multi-PRN-order branch) into a `tests/` suite.

**Known limitations**

- Same-part-of-day episodes from separate reports are merged by the max rule (possible under-count).
- A follow-on sentence without its own date word in a next-morning report ("Yesterday was a good day … Appetite is
  okay.") is dated to the submission day by the LLM; on the full run this created one false
  `cross_report_conflict` (R0034 vs R0037). Same ambiguity as gold case R0175.
- When a patient's medication wording is unmapped, the intake row and the symptom row that mention it each raise
  an `unmapped_medication` flag (R0067 shows the same flag twice); flags should be de-duplicated per report.
- "Overnight" is assigned to the date the night started (convention); questionnaire answers cover the
  submission day.
- Correction context is only the patient's previous report within 72 h.
- LLM self-reported confidence is not calibrated; it is only a secondary flag.
- Red flags are display-prominence rules for review, not clinical triage.
- Questionnaire PRN doses are attributed to the single active PRN order by inference (marked as such); a patient
  could have taken something else (e.g. an over-the-counter medicine). The synthetic data has no day with several
  active PRN orders, so the ambiguous branch is covered by an ad-hoc check, not by the gold set.
- "Last visit" = last infusion day (visits coincide with infusions in the synthetic data).
- Cache keys include model, prompt version, input, context and (for reasoning models) reasoning effort. Entries
  written before effort was added were all produced at `medium`, which therefore adds no suffix, so existing
  cache entries remain valid.

**中文要点：** LLM v2 全量解析已完成（分两天跑完，第一次因每日 token 上限停在 163/184），看板现在显示 LLM v2 的数据；
规则版本的数据库保留在本地 `data/cinv_rule.db`。全量数据上，R0043 同类问题丢掉了 20 行"没有呕吐"，这些天在看板上显示为"没报告"而不是"报告了 0 次"，
所以 v3 是最优先的工作。
R0043 的 v3 方案是改 prompt 让模型引用完整连续的短语，不放宽 evidence 检查；和 v3 一起做的还有"病人给出具体或大概时间时存进
occurred_start"（来自人工复核 R0003、R0067 的意见）。另外还需要一组留出集。
