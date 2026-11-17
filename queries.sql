-- Useful queries over the parsed CINV data. Run them with:
--     .venv\Scripts\python.exe run_queries.py [--name <query name>]
-- Each query starts with a "-- name:" line. All of them read the views in
-- schema.sql, so conflict / correction / vague-count handling is applied
-- consistently (active events only; conflicting counts -> larger value).
-- Results describe patient-reported data; they are not clinical assessments.


-- name: vomiting_per_week
-- Vomiting episodes per patient per study week. Vague counts are kept as a
-- range: episodes_max is NULL when at least one day had an open-ended count
-- ("kept throwing up"), i.e. the true total is ">= episodes_min".
SELECT s.patient_id,
       CAST((julianday(s.occurred_date) - julianday(p.enrolled_at)) / 7 AS INTEGER) + 1 AS study_week,
       SUM(s.episodes_min)                                          AS episodes_min,
       CASE WHEN SUM(s.presence = 'present' AND s.episodes_max IS NULL) > 0 THEN NULL
            ELSE SUM(s.episodes_max) END                             AS episodes_max,
       SUM(s.presence = 'present')                                  AS days_with_vomiting,
       COUNT(*)                                                     AS days_with_data,
       SUM(s.has_open_conflict)                                     AS days_with_open_conflict
FROM v_daily_symptom_summary s
JOIN patients p USING (patient_id)
WHERE s.symptom_code = 'vomiting'
GROUP BY s.patient_id, study_week
ORDER BY s.patient_id, study_week;


-- name: symptoms_before_after_med_change
-- For every medication order that records a change_reason, compare the same
-- cycle phase (acute / delayed) before vs after the change, so the comparison
-- is like-for-like across cycles. Observational only: no causal claim.
WITH changes AS (
    SELECT o.patient_id, m.generic_name AS new_med, o.start_date AS change_date
    FROM medication_orders o JOIN medications m USING (med_id)
    WHERE o.change_reason IS NOT NULL
),
prn AS (
    SELECT patient_id, taken_date, SUM(doses_taken) AS prn_doses
    FROM v_daily_medication_intake
    WHERE is_prn_on_date = 1
    GROUP BY patient_id, taken_date
),
daily AS (
    SELECT s.patient_id, s.occurred_date, s.cinv_phase,
           MAX(CASE WHEN s.symptom_code = 'nausea'   THEN s.max_severity_level END) AS nausea_level,
           SUM(CASE WHEN s.symptom_code = 'vomiting' THEN s.episodes_min END)       AS vomit_min
    FROM v_daily_symptom_summary s
    GROUP BY s.patient_id, s.occurred_date, s.cinv_phase
)
SELECT c.patient_id,
       c.new_med,
       c.change_date,
       CASE WHEN d.occurred_date < c.change_date THEN 'before' ELSE 'after' END AS period,
       d.cinv_phase,
       COUNT(*)                          AS days_with_data,
       ROUND(AVG(d.nausea_level), 2)     AS avg_nausea_level_0_4,
       SUM(d.vomit_min)                  AS vomiting_episodes_min,
       COALESCE(SUM(prn.prn_doses), 0)   AS prn_antiemetic_doses
FROM changes c
JOIN daily d ON d.patient_id = c.patient_id
LEFT JOIN prn ON prn.patient_id = d.patient_id AND prn.taken_date = d.occurred_date
WHERE d.cinv_phase IN ('acute', 'delayed')
GROUP BY c.patient_id, c.new_med, c.change_date, period, d.cinv_phase
ORDER BY c.patient_id, period DESC, d.cinv_phase;


-- name: pre_visit_snapshot
-- The "30 seconds before walking in" view: last 7 days per patient, relative
-- to each patient's latest report. days_reported_7d counts DISTINCT occurred_date (the day the
-- symptoms happened, not the day the report was submitted), so next-morning reports match the chart
-- window. Sorted so patients needing attention come first:
-- open red flags, then other open (non-info) flags, then weight alert, then worst nausea.
WITH latest AS (
    SELECT patient_id, MAX(date(submitted_at)) AS as_of FROM raw_reports GROUP BY patient_id
),
win AS (
    SELECT s.* FROM v_daily_symptom_summary s
    JOIN latest l USING (patient_id)
    WHERE s.occurred_date > date(l.as_of, '-7 days')
)
SELECT l.patient_id,
       p.display_name,
       l.as_of,
       (SELECT COUNT(DISTINCT w.occurred_date) FROM win w
         WHERE w.patient_id = l.patient_id)                                                        AS days_reported_7d,
       (SELECT MAX(w.max_severity_level) FROM win w
         WHERE w.patient_id = l.patient_id AND w.symptom_code = 'nausea')                          AS worst_nausea_0_4,
       (SELECT SUM(w.episodes_min) FROM win w
         WHERE w.patient_id = l.patient_id AND w.symptom_code = 'vomiting')                        AS vomiting_episodes_min,
       (SELECT COUNT(*) FROM win w
         WHERE w.patient_id = l.patient_id AND w.symptom_code = 'unable_to_keep_fluids'
           AND w.presence = 'present')                                                             AS days_not_keeping_fluids,
       (SELECT COALESCE(SUM(i.doses_taken), 0) FROM v_daily_medication_intake i
         WHERE i.patient_id = l.patient_id AND i.is_prn_on_date = 1
           AND i.taken_date > date(l.as_of, '-7 days'))                                            AS prn_doses,
       (SELECT t.pct_change FROM v_weight_trend t
         WHERE t.patient_id = l.patient_id ORDER BY t.measured_date DESC LIMIT 1)                  AS weight_pct_change,
       (SELECT t.below_alert_line FROM v_weight_trend t
         WHERE t.patient_id = l.patient_id ORDER BY t.measured_date DESC LIMIT 1)                  AS below_weight_alert,
       (SELECT COUNT(*) FROM review_flags f
         WHERE f.patient_id = l.patient_id AND f.status = 'open' AND f.flag_type = 'red_flag')     AS open_red_flags,
       (SELECT COUNT(*) FROM review_flags f
         WHERE f.patient_id = l.patient_id AND f.status = 'open' AND f.severity <> 'info')         AS open_flags
FROM latest l
JOIN patients p USING (patient_id)
ORDER BY open_red_flags DESC, open_flags DESC, below_weight_alert DESC, worst_nausea_0_4 DESC;


-- name: review_queue
-- Open review flags, urgent first, with the patient's own words next to the
-- reason, so a reviewer can resolve each one without opening another screen.
SELECT f.flag_id,
       f.severity,
       f.flag_type,
       f.patient_id,
       f.report_id,
       r.submitted_at,
       f.description,
       COALESCE(r.raw_text, json_extract(r.questionnaire_json, '$.comments')) AS patient_words
FROM review_flags f
LEFT JOIN raw_reports r USING (report_id)
WHERE f.status = 'open'
ORDER BY CASE f.severity WHEN 'urgent' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
         r.submitted_at DESC;
