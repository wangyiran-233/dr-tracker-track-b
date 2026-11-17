# Parser evaluation on the gold set - prompt v3

LLM model: openai/gpt-oss-120b. Columns for older prompt versions are re-scored from cached responses, so they match their original runs.

Gold set: 30 reports (23 free text, 7 questionnaires). Questionnaire fields are parsed by deterministic rules in both columns; only their free-text comments differ.

## Free-text reports (n=23)

| Metric | Rule baseline | LLM prompt v1 | LLM prompt v2 | LLM prompt v3 |
|---|---|---|---|---|
| Symptom detection - precision | 100% (39/39) | 100% (39/39) | 100% (39/39) | 100% (40/40) |
| Symptom detection - recall | 98% (39/40) | 98% (39/40) | 98% (39/40) | 100% (40/40) |
| Presence (present/absent/uncertain) | 97% (38/39) | 100% (39/39) | 100% (39/39) | 98% (39/40) |
| Episode count (min & max) | 93% (14/15) | 100% (15/15) | 100% (15/15) | 94% (15/16) |
| Severity level | 94% (17/18) | 100% (18/18) | 100% (18/18) | 94% (17/18) |
| Date (occurred_date) | 100% (39/39) | 100% (39/39) | 97% (38/39) | 100% (40/40) |
| Part of day | 90% (35/39) | 85% (33/39) | 92% (36/39) | 92% (37/40) |
| Clock time when the patient states one (incl. ~approx.) | 0% (0/4) | 0% (0/4) | 0% (0/4) | 75% (3/4) |
| Medication intake (drug, status, date, inferred vs stated) - precision | 100% (6/6) | 100% (6/6) | 100% (6/6) | 100% (6/6) |
| Medication intake (drug, status, date, inferred vs stated) - recall | 100% (6/6) | 100% (6/6) | 100% (6/6) | 100% (6/6) |
| Review flags - precision | 100% (4/4) | 100% (4/4) | 100% (5/5) | 100% (5/5) |
| Review flags - recall | 80% (4/5) | 80% (4/5) | 100% (5/5) | 100% (5/5) |
| Correction linked to original | 0% (0/1) | 0% (0/1) | 100% (1/1) | 100% (1/1) |
| Report fully correct - hand-annotated tricky | 69% (11/16) | 69% (11/16) | 75% (12/16) | 75% (12/16) |
| Report fully correct - routine | 100% (7/7) | 86% (6/7) | 71% (5/7) | 100% (7/7) |

Symptom detection F1: Rule baseline 0.99, LLM prompt v1 0.99, LLM prompt v2 0.99, LLM prompt v3 1.00

Reports where the parser produced no usable output (scored as all-missed): Rule baseline 0, LLM prompt v1 0, LLM prompt v2 0, LLM prompt v3 0

## Questionnaire reports (n=7)

| Metric | Rule baseline | LLM prompt v1 | LLM prompt v2 | LLM prompt v3 |
|---|---|---|---|---|
| Symptom detection - precision | 100% (29/29) | 100% (29/29) | 100% (29/29) | 100% (29/29) |
| Symptom detection - recall | 100% (29/29) | 100% (29/29) | 100% (29/29) | 100% (29/29) |
| Presence (present/absent/uncertain) | 100% (29/29) | 100% (29/29) | 100% (29/29) | 100% (29/29) |
| Episode count (min & max) | 100% (8/8) | 100% (8/8) | 100% (8/8) | 100% (8/8) |
| Severity level | 100% (10/10) | 100% (10/10) | 100% (10/10) | 100% (10/10) |
| Date (occurred_date) | 100% (29/29) | 100% (29/29) | 100% (29/29) | 100% (29/29) |
| Part of day | 100% (29/29) | 100% (29/29) | 100% (29/29) | 100% (29/29) |
| Medication intake (drug, status, date, inferred vs stated) - precision | 100% (2/2) | 100% (2/2) | 100% (2/2) | 100% (2/2) |
| Medication intake (drug, status, date, inferred vs stated) - recall | 100% (2/2) | 100% (2/2) | 100% (2/2) | 100% (2/2) |
| Review flags - precision | 100% (2/2) | 100% (2/2) | 100% (2/2) | 100% (2/2) |
| Review flags - recall | 100% (2/2) | 100% (2/2) | 100% (2/2) | 100% (2/2) |
| Report fully correct - hand-annotated tricky | 100% (2/2) | 100% (2/2) | 100% (2/2) | 100% (2/2) |
| Report fully correct - routine | 100% (5/5) | 100% (5/5) | 100% (5/5) | 100% (5/5) |

## Errors - Rule baseline (6 reports with at least one error)

**R0003** (free_text; vomited_up_dose)  
> Took my ondansetron at 8 this morning but threw it up about 20 minutes later. Took another at 10 and that one stayed down.

- `WRONG      vomiting: part_of_day expected morning got unknown, count expected 1-1 got 1-None, clock_time expected ~08:20 got none  evidence='threw it up about 20 minutes later.'`
- `WRONG      intake ondansetron vomited_up: clock_time expected 08:00 got none`
- `WRONG      intake ondansetron taken: clock_time expected 10:00 got none`

**R0009** (free_text; correction)  
> Correction to yesterday: it was actually 3 times, not once. I was too wiped out to remember.

- `MISSED     vomiting present 2026-09-10 unknown n=3-3`
- `CORRECTION not linked to R0007`

**R0067** (free_text; unmapped_medication)  
> Took the little pill that melts on my tongue around 2pm, nausea eased after an hour.

- `WRONG      nausea: part_of_day expected afternoon got unknown  evidence='nausea eased after an hour.'`
- `WRONG      intake None taken: clock_time expected ~14:00 got none`

**R0087** (free_text; negation, hedged_severity)  
> No throwing up today. Nausea pretty much gone, maybe a tiny bit after breakfast.

- `WRONG      nausea: presence expected present got absent, part_of_day expected morning got unknown, severity expected 1 got 0  evidence='Nausea pretty much gone'`

**R0089** (free_text; implausible_value)  
> Threw up like a hundred times today, it just would not stop.

- `FLAG MISSED implausible_value`

**R0117** (free_text; severity_mismatch, red_flag)  
> Nausea is mild I guess, but I haven't been able to keep any water down since this morning. Threw up 4 times.

- `WRONG      unable_to_keep_fluids: part_of_day expected all_day got morning  evidence="I haven't been able to keep any water down since this morning."`

## Errors - LLM prompt v1 (7 reports with at least one error)

**R0003** (free_text; vomited_up_dose)  
> Took my ondansetron at 8 this morning but threw it up about 20 minutes later. Took another at 10 and that one stayed down.

- `WRONG      vomiting: clock_time expected ~08:20 got none  evidence='threw it up about 20 minutes later'`
- `WRONG      intake ondansetron vomited_up: clock_time expected 08:00 got none`
- `WRONG      intake ondansetron taken: clock_time expected 10:00 got none`

**R0009** (free_text; correction)  
> Correction to yesterday: it was actually 3 times, not once. I was too wiped out to remember.

- `MISSED     vomiting present 2026-09-10 unknown n=3-3`
- `CORRECTION not linked to R0007`

**R0026** (free_text; despite_medication, brand_name)  
> Still really nauseous even after taking Zofran at lunch. No vomiting.

- `WRONG      nausea: part_of_day expected afternoon got unknown  evidence='Still really nauseous even after taking Zofran'`

**R0043** (free_text; routine, evening)  
> Feeling pretty good today, no nausea or vomiting.

- `WRONG      nausea: part_of_day expected unknown got evening  evidence='no nausea'`
- `WRONG      vomiting: part_of_day expected unknown got evening  evidence='no nausea or vomiting'`

**R0067** (free_text; unmapped_medication)  
> Took the little pill that melts on my tongue around 2pm, nausea eased after an hour.

- `WRONG      nausea: part_of_day expected afternoon got unknown  evidence='nausea eased after an hour'`
- `WRONG      intake None taken: clock_time expected ~14:00 got none`

**R0089** (free_text; implausible_value)  
> Threw up like a hundred times today, it just would not stop.

- `FLAG MISSED implausible_value`

**R0117** (free_text; severity_mismatch, red_flag)  
> Nausea is mild I guess, but I haven't been able to keep any water down since this morning. Threw up 4 times.

- `WRONG      nausea: part_of_day expected unknown got evening  evidence='Nausea is mild'`
- `WRONG      unable_to_keep_fluids: part_of_day expected all_day got unknown  evidence="haven't been able to keep any water down"`

## Errors - LLM prompt v2 (7 reports with at least one error)

**R0003** (free_text; vomited_up_dose)  
> Took my ondansetron at 8 this morning but threw it up about 20 minutes later. Took another at 10 and that one stayed down.

- `WRONG      vomiting: clock_time expected ~08:20 got none  evidence='threw it up about 20 minutes later'`
- `WRONG      intake ondansetron vomited_up: clock_time expected 08:00 got none`
- `WRONG      intake ondansetron taken: clock_time expected 10:00 got none`

**R0026** (free_text; despite_medication, brand_name)  
> Still really nauseous even after taking Zofran at lunch. No vomiting.

- `WRONG      nausea: part_of_day expected afternoon got unknown  evidence='still really nauseous even after taking Zofran'`

**R0043** (free_text; routine, evening)  
> Feeling pretty good today, no nausea or vomiting.

- `MISSED     vomiting absent 2026-09-17 unknown n=0-0`
- `DROPPED    symptom vomiting: evidence not in text: 'no vomiting'`

**R0055** (free_text; weight_in_text)  
> Weighed myself this morning: 72.1 kg. Appetite still poor.

- `WRONG      appetite_loss: part_of_day expected unknown got morning  evidence='Appetite still poor'`

**R0067** (free_text; unmapped_medication)  
> Took the little pill that melts on my tongue around 2pm, nausea eased after an hour.

- `WRONG      intake None taken: clock_time expected ~14:00 got none`

**R0117** (free_text; severity_mismatch, red_flag)  
> Nausea is mild I guess, but I haven't been able to keep any water down since this morning. Threw up 4 times.

- `WRONG      unable_to_keep_fluids: part_of_day expected all_day got morning  evidence="haven't been able to keep any water down since this morning"`

**R0175** (free_text; routine, next_morning)  
> Yesterday was a good day, no nausea or vomiting. Appetite is okay.

- `WRONG      appetite_loss: date expected 2026-10-18 got 2026-10-19  evidence='Appetite is okay'`

## Errors - LLM prompt v3 (4 reports with at least one error)

**R0003** (free_text; vomited_up_dose)  
> Took my ondansetron at 8 this morning but threw it up about 20 minutes later. Took another at 10 and that one stayed down.

- `WRONG      vomiting: count expected 1-1 got 1-None, clock_time expected ~08:20 got none  evidence='threw it up about 20 minutes later'`

**R0087** (free_text; negation, hedged_severity)  
> No throwing up today. Nausea pretty much gone, maybe a tiny bit after breakfast.

- `WRONG      nausea: presence expected present got uncertain, severity expected 1 got None  evidence='Nausea pretty much gone, maybe a tiny bit after breakfast'`

**R0117** (free_text; severity_mismatch, red_flag)  
> Nausea is mild I guess, but I haven't been able to keep any water down since this morning. Threw up 4 times.

- `WRONG      nausea: part_of_day expected unknown got morning  evidence='Nausea is mild'`
- `WRONG      unable_to_keep_fluids: part_of_day expected all_day got morning  evidence="haven't been able to keep any water down since this morning"`

**R0133** (free_text; duplicate_original)  
> Mild nausea after lunch, no vomiting.

- `WRONG      vomiting: part_of_day expected unknown got afternoon  evidence='no vomiting'`
