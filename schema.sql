-- =====================================================================
-- Dr.Tracker Track B prototype: CINV (chemotherapy-induced nausea and
-- vomiting) patient-reported data schema. SQLite.
--
-- Design principles
--   1. Raw patient reports are immutable; everything structured is derived
--      from them and traceable back via report_id + evidence_span.
--   2. Unknown stays NULL. Vague amounts are stored as ranges
--      (episode_count_min/max) and time as (part_of_day, time_precision).
--   3. Conflicts are never silently resolved in the tables: both versions
--      are kept and flagged in review_flags. The daily summary view applies
--      one explicit, conservative rule (take the larger count + mark it).
--   4. Event (long) model: one report -> many events.
--
-- Clinical terms (acute / delayed phase, emetogenic risk, severity bands)
-- follow commonly used definitions for reference only. Nothing here is a
-- diagnosis or a clinical grade (e.g. CTCAE grades are clinician-assigned
-- and deliberately not stored).
-- All dates are ISO-8601 text: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS'
-- (local clinic time, no timezone in this prototype).
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Reference / clinical context
-- ---------------------------------------------------------------------

CREATE TABLE patients (
    patient_id          TEXT PRIMARY KEY,                 -- synthetic, e.g. 'P001'
    display_name        TEXT NOT NULL,                    -- fictional, demo only
    diagnosis           TEXT NOT NULL,
    regimen_name        TEXT NOT NULL,
    emetogenic_risk     TEXT NOT NULL
                        CHECK (emetogenic_risk IN ('high', 'moderate', 'low')),
    enrolled_at         TEXT NOT NULL,
    baseline_weight_kg  REAL CHECK (baseline_weight_kg BETWEEN 20 AND 300),
    -- provider-configured alert line (% loss from baseline); a practice
    -- setting, not a clinical recommendation
    weight_alert_pct    REAL NOT NULL DEFAULT 5.0
                        CHECK (weight_alert_pct > 0 AND weight_alert_pct < 50)
);

-- Time anchor: lets every symptom be placed at "day N of cycle".
CREATE TABLE chemo_administrations (
    admin_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id       TEXT NOT NULL REFERENCES patients(patient_id),
    cycle_number     INTEGER NOT NULL CHECK (cycle_number >= 1),
    administered_at  TEXT NOT NULL,
    agents           TEXT NOT NULL,
    UNIQUE (patient_id, cycle_number)
);

CREATE TABLE medications (
    med_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    generic_name  TEXT NOT NULL UNIQUE,
    brand_names   TEXT NOT NULL DEFAULT '[]',   -- JSON array, used by parser for name matching
    drug_class    TEXT NOT NULL,
    is_antiemetic INTEGER NOT NULL CHECK (is_antiemetic IN (0, 1))
);

-- A medication change = close the old order (end_date) + open a new one.
-- Never overwrite, so before/after comparisons stay possible.
CREATE TABLE medication_orders (
    order_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     TEXT NOT NULL REFERENCES patients(patient_id),
    med_id         INTEGER NOT NULL REFERENCES medications(med_id),
    dose           TEXT NOT NULL,
    route          TEXT NOT NULL,
    frequency      TEXT NOT NULL,
    is_prn         INTEGER NOT NULL CHECK (is_prn IN (0, 1)),
    start_date     TEXT NOT NULL,
    end_date       TEXT,                         -- NULL = still active
    change_reason  TEXT,
    CHECK (end_date IS NULL OR end_date >= start_date)
);

-- ---------------------------------------------------------------------
-- Raw input (immutable)
-- ---------------------------------------------------------------------

CREATE TABLE raw_reports (
    report_id           TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    submitted_at        TEXT NOT NULL,           -- system time; NOT when the symptom happened
    source              TEXT NOT NULL CHECK (source IN ('free_text', 'questionnaire')),
    raw_text            TEXT,
    questionnaire_json  TEXT,                    -- answers kept as submitted (0-10 scores unchanged)
    parse_status        TEXT NOT NULL DEFAULT 'pending'
                        CHECK (parse_status IN ('pending', 'parsed', 'failed', 'needs_review')),
    CHECK ((source = 'free_text'     AND raw_text IS NOT NULL) OR
           (source = 'questionnaire' AND questionnaire_json IS NOT NULL))
);

CREATE TRIGGER raw_reports_content_immutable
BEFORE UPDATE OF patient_id, submitted_at, source, raw_text, questionnaire_json ON raw_reports
BEGIN
    SELECT RAISE(ABORT, 'raw_reports content is immutable; only parse_status may change');
END;

CREATE TRIGGER raw_reports_no_delete
BEFORE DELETE ON raw_reports
BEGIN
    SELECT RAISE(ABORT, 'raw_reports rows cannot be deleted');
END;

-- One row per parser / model / prompt version, so runs can be compared
-- against the gold set and re-run.
CREATE TABLE extraction_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    method          TEXT NOT NULL CHECK (method IN ('questionnaire', 'rule', 'llm', 'manual')),
    model_name      TEXT,
    prompt_version  TEXT,
    run_at          TEXT NOT NULL,
    notes           TEXT
);

-- ---------------------------------------------------------------------
-- Derived events
-- ---------------------------------------------------------------------

CREATE TABLE symptom_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           TEXT NOT NULL REFERENCES raw_reports(report_id),
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    symptom_code        TEXT NOT NULL CHECK (symptom_code IN (
                            'nausea', 'vomiting', 'retching',
                            'appetite_loss', 'unable_to_keep_fluids')),
    -- 'absent' is real data ("no vomiting today"), not the same as "not reported"
    presence            TEXT NOT NULL CHECK (presence IN ('present', 'absent', 'uncertain')),
    -- 0-4 verbal scale: none / mild / moderate / severe / very severe. NULL if not stated.
    severity_level      INTEGER CHECK (severity_level BETWEEN 0 AND 4),
    -- questionnaire 0-10 score, stored exactly as answered
    severity_nrs        INTEGER CHECK (severity_nrs BETWEEN 0 AND 10),
    severity_raw        TEXT,                    -- patient's own words, e.g. 'pretty bad'
    -- vague counts become ranges: 'twice' 2/2, 'a few times' 2/4,
    -- 'kept throwing up' 1/NULL (at least one, upper bound unknown)
    episode_count_min   INTEGER CHECK (episode_count_min >= 0),
    episode_count_max   INTEGER CHECK (episode_count_max >= 0),
    occurred_date       TEXT NOT NULL,           -- resolved from relative words using submitted_at
    part_of_day         TEXT NOT NULL DEFAULT 'unknown' CHECK (part_of_day IN (
                            'overnight', 'morning', 'afternoon', 'evening', 'all_day', 'unknown')),
    occurred_start      TEXT,                    -- only when the patient gave a clock time
    occurred_end        TEXT,
    time_precision      TEXT NOT NULL DEFAULT 'day'
                        CHECK (time_precision IN ('exact', 'part_of_day', 'day', 'approximate')),
    timing_raw          TEXT,                    -- e.g. 'after dinner'
    related_med_id      INTEGER REFERENCES medications(med_id),
    med_relation        TEXT CHECK (med_relation IN (
                            'after_taking', 'despite_taking', 'before_taking', 'vomited_up_dose')),
    evidence_span       TEXT,                    -- quote from the raw report supporting this row
    extraction_method   TEXT NOT NULL CHECK (extraction_method IN ('questionnaire', 'rule', 'llm', 'manual')),
    extraction_run_id   INTEGER REFERENCES extraction_runs(run_id),
    confidence          REAL CHECK (confidence BETWEEN 0 AND 1),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'superseded', 'rejected')),
    supersedes_event_id INTEGER REFERENCES symptom_events(event_id),
    CHECK (episode_count_max IS NULL OR episode_count_min IS NULL
           OR episode_count_max >= episode_count_min),
    CHECK (presence <> 'absent' OR severity_level IS NULL OR severity_level = 0)
);

CREATE INDEX idx_symptom_patient_day ON symptom_events (patient_id, occurred_date, symptom_code);
CREATE INDEX idx_symptom_report      ON symptom_events (report_id);

-- Antiemetic use is itself a signal (more PRN doses = worse control);
-- missed / vomited-up doses change how symptoms should be read.
CREATE TABLE medication_intake_events (
    intake_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           TEXT NOT NULL REFERENCES raw_reports(report_id),
    patient_id          TEXT NOT NULL REFERENCES patients(patient_id),
    med_id              INTEGER REFERENCES medications(med_id),   -- NULL = could not be mapped
    med_text_raw        TEXT,                                     -- e.g. 'the little pill that melts'
    intake_status       TEXT NOT NULL CHECK (intake_status IN ('taken', 'missed', 'vomited_up', 'unknown')),
    dose_count          INTEGER NOT NULL DEFAULT 1 CHECK (dose_count >= 0),
    taken_date          TEXT NOT NULL,
    part_of_day         TEXT NOT NULL DEFAULT 'unknown' CHECK (part_of_day IN (
                            'overnight', 'morning', 'afternoon', 'evening', 'all_day', 'unknown')),
    time_precision      TEXT NOT NULL DEFAULT 'day'
                        CHECK (time_precision IN ('exact', 'part_of_day', 'day', 'approximate')),
    evidence_span       TEXT,
    extraction_method   TEXT NOT NULL CHECK (extraction_method IN ('questionnaire', 'rule', 'llm', 'manual')),
    extraction_run_id   INTEGER REFERENCES extraction_runs(run_id),
    confidence          REAL CHECK (confidence BETWEEN 0 AND 1),
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'superseded', 'rejected')),
    supersedes_intake_id INTEGER REFERENCES medication_intake_events(intake_id)
);

CREATE INDEX idx_intake_patient_day ON medication_intake_events (patient_id, taken_date);

CREATE TABLE weight_measurements (
    weight_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      TEXT NOT NULL REFERENCES patients(patient_id),
    measured_date   TEXT NOT NULL,
    weight_kg       REAL NOT NULL CHECK (weight_kg BETWEEN 20 AND 300),
    source          TEXT NOT NULL CHECK (source IN ('clinic', 'patient_reported')),
    report_id       TEXT REFERENCES raw_reports(report_id),       -- set when patient-reported
    evidence_span   TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'superseded', 'rejected')),
    CHECK (source = 'clinic' OR report_id IS NOT NULL)
);

CREATE INDEX idx_weight_patient_day ON weight_measurements (patient_id, measured_date);

-- ---------------------------------------------------------------------
-- Data quality / review queue
-- ---------------------------------------------------------------------

CREATE TABLE review_flags (
    flag_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id       TEXT NOT NULL REFERENCES patients(patient_id),
    report_id        TEXT REFERENCES raw_reports(report_id),
    flag_type        TEXT NOT NULL CHECK (flag_type IN (
                         'intra_report_conflict',   -- e.g. questionnaire says 0 vomits, comment says 1
                         'cross_report_conflict',   -- two reports disagree about the same day/window
                         'severity_mismatch',       -- 'mild' but cannot keep water down
                         'low_confidence',
                         'unmapped_medication',
                         'implausible_value',       -- e.g. 'threw up a hundred times'
                         'duplicate_report',
                         'red_flag')),              -- surfaced at top of dashboard; not a diagnosis
    severity         TEXT NOT NULL DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'urgent')),
    description      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolution_note  TEXT,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);

-- Which events a flag is about (junction table instead of a JSON array,
-- so views can join on it).
CREATE TABLE flag_events (
    flag_id      INTEGER NOT NULL REFERENCES review_flags(flag_id),
    event_table  TEXT NOT NULL CHECK (event_table IN ('symptom', 'intake', 'weight')),
    event_id     INTEGER NOT NULL,
    PRIMARY KEY (flag_id, event_table, event_id)
);

-- ---------------------------------------------------------------------
-- Views for queries and the dashboard
-- ---------------------------------------------------------------------

-- One row per patient / day / symptom.
--
-- Counting rule for episodes (documented trade-off):
--   * Events in the SAME part of day from different reports are treated as
--     the same episodes -> take the larger count (conservative: missing a
--     vomiting episode is worse than over-counting) and expose
--     has_open_conflict so the provider sees the number is disputed.
--   * Specific, non-overlapping parts of day (morning / afternoon / ...)
--     are added up.
--   * 'all_day' (questionnaire 24h window) and 'unknown' overlap everything,
--     so the day total = max(largest all_day/unknown count, sum of specific parts).
--   * Superseded / rejected events are excluded.
--   Known limitation: two separate episodes reported in two reports for the
--   same part of day will be under-counted.
--
-- Cycle phase uses commonly used reference definitions at day granularity:
--   day_of_cycle 1 (infusion day) = 'acute' (~first 24h),
--   days 2-5 = 'delayed' (~24-120h), later = 'between_cycles',
--   before first infusion = 'pre_treatment'. Approximation, not a clinical label.
CREATE VIEW v_daily_symptom_summary AS
WITH active AS (
    SELECT * FROM symptom_events WHERE status = 'active'
),
per_part AS (
    SELECT patient_id, occurred_date, symptom_code, part_of_day,
           MAX(CASE presence WHEN 'present' THEN 2 WHEN 'uncertain' THEN 1 ELSE 0 END) AS presence_rank,
           -- severity only from non-absent rows, so an 'absent' 0 can't mask a present episode
           MAX(CASE WHEN presence <> 'absent' THEN severity_level END) AS max_sev,
           MAX(CASE WHEN presence <> 'absent' THEN severity_nrs END)   AS max_nrs,
           MAX(severity_nrs)      AS any_nrs,     -- lets an all-absent day still show a reported 0
           MAX(episode_count_min) AS cmin,
           CASE WHEN SUM(episode_count_min IS NOT NULL AND episode_count_max IS NULL) > 0
                THEN NULL ELSE MAX(episode_count_max) END AS cmax,
           SUM(episode_count_min IS NOT NULL AND episode_count_max IS NULL) AS open_ended,
           MAX(CASE WHEN presence = 'uncertain'
                      OR (episode_count_min IS NOT NULL AND
                          (episode_count_max IS NULL OR episode_count_max <> episode_count_min))
                    THEN 1 ELSE 0 END) AS imprecise
    FROM active
    GROUP BY patient_id, occurred_date, symptom_code, part_of_day
),
per_day AS (
    SELECT patient_id, occurred_date, symptom_code,
           MAX(presence_rank) AS presence_rank,
           CASE WHEN MAX(presence_rank) = 0 THEN 0 ELSE MAX(max_sev) END AS max_severity_level,
           CASE WHEN MAX(presence_rank) = 0 THEN MAX(any_nrs) ELSE MAX(max_nrs) END AS max_severity_nrs,
           CASE WHEN COUNT(cmin) = 0 THEN NULL ELSE
               MAX(COALESCE(MAX(CASE WHEN part_of_day IN ('all_day', 'unknown') THEN cmin END), 0),
                   COALESCE(SUM(CASE WHEN part_of_day NOT IN ('all_day', 'unknown') THEN cmin END), 0))
           END AS episodes_min,
           CASE WHEN COUNT(cmin) = 0 THEN NULL
                WHEN SUM(open_ended) > 0 THEN NULL ELSE
               MAX(COALESCE(MAX(CASE WHEN part_of_day IN ('all_day', 'unknown') THEN cmax END), 0),
                   COALESCE(SUM(CASE WHEN part_of_day NOT IN ('all_day', 'unknown') THEN cmax END), 0))
           END AS episodes_max,
           MAX(imprecise) AS has_imprecise
    FROM per_part
    GROUP BY patient_id, occurred_date, symptom_code
)
SELECT d.patient_id,
       d.occurred_date,
       d.symptom_code,
       CASE d.presence_rank WHEN 2 THEN 'present' WHEN 1 THEN 'uncertain' ELSE 'absent' END AS presence,
       d.max_severity_level,
       d.max_severity_nrs,
       d.episodes_min,
       d.episodes_max,
       d.has_imprecise,
       (SELECT COUNT(DISTINCT e.report_id) FROM active e
         WHERE e.patient_id = d.patient_id AND e.occurred_date = d.occurred_date
           AND e.symptom_code = d.symptom_code) AS n_reports,
       EXISTS (SELECT 1 FROM review_flags f
                 JOIN flag_events fe ON fe.flag_id = f.flag_id AND fe.event_table = 'symptom'
                 JOIN active e ON e.event_id = fe.event_id
                WHERE f.status = 'open' AND f.flag_type <> 'red_flag'
                  AND e.patient_id = d.patient_id AND e.occurred_date = d.occurred_date
                  AND e.symptom_code = d.symptom_code) AS has_open_conflict,
       EXISTS (SELECT 1 FROM review_flags f
                 JOIN flag_events fe ON fe.flag_id = f.flag_id AND fe.event_table = 'symptom'
                 JOIN active e ON e.event_id = fe.event_id
                WHERE f.status = 'open' AND f.flag_type = 'red_flag'
                  AND e.patient_id = d.patient_id AND e.occurred_date = d.occurred_date
                  AND e.symptom_code = d.symptom_code) AS has_red_flag,
       c.cycle_number,
       CAST(julianday(d.occurred_date) - julianday(date(c.administered_at)) AS INTEGER) + 1 AS day_of_cycle,
       CASE
           WHEN c.admin_id IS NULL THEN 'pre_treatment'
           WHEN julianday(d.occurred_date) - julianday(date(c.administered_at)) < 1 THEN 'acute'
           WHEN julianday(d.occurred_date) - julianday(date(c.administered_at)) < 5 THEN 'delayed'
           ELSE 'between_cycles'
       END AS cinv_phase
FROM per_day d
LEFT JOIN chemo_administrations c
       ON c.admin_id = (SELECT c2.admin_id FROM chemo_administrations c2
                         WHERE c2.patient_id = d.patient_id
                           AND date(c2.administered_at) <= d.occurred_date
                         ORDER BY c2.administered_at DESC LIMIT 1);

-- Daily medication use; is_prn reflects the order active on that date,
-- so "PRN doses per day" can be charted next to symptoms.
CREATE VIEW v_daily_medication_intake AS
SELECT i.patient_id,
       i.taken_date,
       m.generic_name,
       m.is_antiemetic,
       EXISTS (SELECT 1 FROM medication_orders o
                WHERE o.patient_id = i.patient_id AND o.med_id = i.med_id AND o.is_prn = 1
                  AND o.start_date <= i.taken_date
                  AND (o.end_date IS NULL OR o.end_date >= i.taken_date)) AS is_prn_on_date,
       SUM(CASE WHEN i.intake_status = 'taken'      THEN i.dose_count ELSE 0 END) AS doses_taken,
       SUM(CASE WHEN i.intake_status = 'missed'     THEN i.dose_count ELSE 0 END) AS doses_missed,
       SUM(CASE WHEN i.intake_status = 'vomited_up' THEN i.dose_count ELSE 0 END) AS doses_vomited_up
FROM medication_intake_events i
LEFT JOIN medications m ON m.med_id = i.med_id
WHERE i.status = 'active'
GROUP BY i.patient_id, i.taken_date, i.med_id;

-- Weight trend vs. the provider-configured alert line.
CREATE VIEW v_weight_trend AS
SELECT w.patient_id,
       w.measured_date,
       w.weight_kg,
       w.source,
       p.baseline_weight_kg,
       ROUND(100.0 * (w.weight_kg - p.baseline_weight_kg) / p.baseline_weight_kg, 1) AS pct_change,
       ROUND(p.baseline_weight_kg * (1 - p.weight_alert_pct / 100.0), 1)            AS alert_line_kg,
       (w.weight_kg <= p.baseline_weight_kg * (1 - p.weight_alert_pct / 100.0))     AS below_alert_line
FROM weight_measurements w
JOIN patients p ON p.patient_id = w.patient_id
WHERE w.status = 'active';
