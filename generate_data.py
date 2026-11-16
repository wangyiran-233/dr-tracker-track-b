"""Generate the synthetic CINV dataset and the parser gold set.

Outputs
  data/synthetic_dataset.json  patients, medications, orders, chemo days,
                               clinic weights, raw patient reports
  data/gold_set.json           30 reports with expected structured output

Everything here is synthetic. Regimens and antiemetic orders are simplified
for the demo and are NOT clinical guidance. Deterministic (fixed seed).

How the data is made
  * A hidden "truth" curve per patient/day: nausea peaks around days 2-3
    after each infusion (shape follows the commonly described acute/delayed
    pattern, for reference only), scaled by the regimen's emetogenic risk.
  * Routine reports are rendered from that truth by templates, so their gold
    labels come straight from the generator (label_source = 'generator').
  * 15 hand-written tricky cases (vague, conflicting, corrections, ...) are
    written with their labels by hand (label_source = 'hand_annotated').
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

SEED = 42
STUDY_DAYS = 42
OUT_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

MEDICATIONS = [
    # generic, brand names, class, is_antiemetic
    ("ondansetron", ["Zofran", "Zofran ODT"], "5-HT3 antagonist", 1),
    ("olanzapine", ["Zyprexa"], "atypical antipsychotic", 1),
    ("dexamethasone", ["Decadron"], "corticosteroid", 1),
    ("prochlorperazine", ["Compazine"], "dopamine antagonist", 1),
    ("aprepitant", ["Emend"], "NK1 antagonist", 1),
    ("metoclopramide", ["Reglan"], "dopamine antagonist", 1),
    ("cisplatin", [], "chemotherapy (platinum)", 0),
    ("carboplatin", ["Paraplatin"], "chemotherapy (platinum)", 0),
    ("paclitaxel", ["Taxol"], "chemotherapy (taxane)", 0),
    ("docetaxel", ["Taxotere"], "chemotherapy (taxane)", 0),
    ("fluorouracil", ["5-FU"], "chemotherapy (antimetabolite)", 0),
]

# How patients refer to their PRN antiemetic in routine free text.
PRN_NAMES = {
    "ondansetron": ["Zofran", "ondansetron", "my Zofran"],
    "prochlorperazine": ["Compazine", "prochlorperazine"],
}


@dataclass
class Patient:
    patient_id: str
    display_name: str
    diagnosis: str
    regimen_name: str
    risk: str
    start: date
    infusion_days: list[int]          # study days (1-based)
    agents: str
    baseline_weight: float
    peak: float                       # peak nausea (0-10) in the truth model
    prn_med: str
    report_rate: float
    switch_day: int | None = None     # P002: olanzapine added on this study day
    truth: dict = field(default_factory=dict)

    def day(self, k: int) -> date:
        return self.start + timedelta(days=k - 1)

    def iso(self, k: int) -> str:
        return self.day(k).isoformat()


PATIENTS = [
    Patient("P001", "Alex R. (synthetic)", "Oropharyngeal SCC",
            "Cisplatin 100 mg/m2 q21d + RT", "high", date(2026, 9, 7), [1, 22],
            "cisplatin", 74.0, 7.0, "ondansetron", 0.85),
    Patient("P002", "Jordan M. (synthetic)", "Laryngeal SCC",
            "Cisplatin 100 mg/m2 q21d + RT", "high", date(2026, 9, 8), [1, 22],
            "cisplatin", 81.5, 8.0, "ondansetron", 0.85, switch_day=9),
    Patient("P003", "Sam K. (synthetic)", "Nasopharyngeal carcinoma",
            "Carboplatin + paclitaxel q14d (simplified)", "moderate", date(2026, 9, 9), [1, 15, 29],
            "carboplatin, paclitaxel", 63.2, 5.0, "ondansetron", 0.80),
    Patient("P004", "Taylor B. (synthetic)", "Oral cavity SCC",
            "Paclitaxel q14d + RT (simplified)", "low", date(2026, 9, 10), [1, 15, 29],
            "paclitaxel", 69.0, 2.5, "prochlorperazine", 0.65),
    Patient("P005", "Casey L. (synthetic)", "Hypopharyngeal SCC",
            "TPF (docetaxel + cisplatin + 5-FU) q21d", "high", date(2026, 9, 11), [2, 23],
            "docetaxel, cisplatin, fluorouracil", 77.8, 7.5, "ondansetron", 0.85),
]
PBY = {p.patient_id: p for p in PATIENTS}

# ---------------------------------------------------------------------------
# Conventions shared by generator, gold set and (later) the parser
# ---------------------------------------------------------------------------

CONVENTIONS = {
    "scope": "Only the 5 CINV symptoms are extracted: nausea, vomiting, retching, "
             "appetite_loss, unable_to_keep_fluids.",
    "severity_level": "0-4 = none/mild/moderate/severe/very severe. NULL when the patient "
                      "gives no intensity word (e.g. 'nausea is worse').",
    "nrs_to_level": "Questionnaire 0-10 kept as severity_nrs; severity_level derived as "
                    "0->0, 1-3->1, 4-6->2, 7-8->3, 9-10->4 (bands follow a commonly used "
                    "mild/moderate/severe split; the 7-8 vs 9-10 split is a project convention).",
    "absent": "presence='absent' => severity_level 0; for vomiting/retching counts 0/0.",
    "counts": "'once' 1/1, 'twice' 2/2, 'a few times' 2/4, present but no number => 1/NULL "
              "(at least one), implausible numbers => 1/NULL + implausible_value flag.",
    "time": "occurred_date is resolved from relative words using submitted_at. "
            "'overnight' belongs to the date the night started. Questionnaire answers cover "
            "the submission day and use part_of_day='all_day'. No time words => 'unknown'.",
    "questionnaire_prn": "prn_antiemetic_doses_24h maps to the patient's active PRN antiemetic.",
    "fluids": "kept_fluids_down: yes => unable_to_keep_fluids absent; partly => present, "
              "severity NULL; no => present + red_flag.",
    "flags": "'flags' are detectable from the report alone; 'cross_report_flags' need other "
             "reports (conflict / duplicate checker).",
}


def nrs_to_level(nrs: int) -> int:
    if nrs == 0:
        return 0
    if nrs <= 3:
        return 1
    if nrs <= 6:
        return 2
    if nrs <= 8:
        return 3
    return 4


def sym(code, presence, occurred_date, part="unknown", level=None, nrs=None,
        cmin=None, cmax=None, related_med=None, med_relation=None):
    return {
        "symptom_code": code, "presence": presence,
        "severity_level": level, "severity_nrs": nrs,
        "episode_count_min": cmin, "episode_count_max": cmax,
        "occurred_date": occurred_date, "part_of_day": part,
        "related_med": related_med, "med_relation": med_relation,
    }


def intake(med, status, taken_date, part="unknown", dose_count=1):
    return {"med": med, "intake_status": status, "dose_count": dose_count,
            "taken_date": taken_date, "part_of_day": part}


def expected(symptoms=(), intakes=(), weights=(), flags=(), cross_flags=(), supersedes=None):
    return {
        "symptom_events": list(symptoms),
        "intake_events": list(intakes),
        "weights": list(weights),
        "flags": list(flags),
        "cross_report_flags": list(cross_flags),
        "supersedes_report_key": supersedes,   # replaced by report_id at the end
    }


# ---------------------------------------------------------------------------
# Truth model
# ---------------------------------------------------------------------------

CURVE = {0: 0.6, 1: 0.85, 2: 1.0, 3: 0.8, 4: 0.5, 5: 0.3, 6: 0.15, 7: 0.1}


def days_since_infusion(p: Patient, k: int) -> int | None:
    past = [d for d in p.infusion_days if d <= k]
    return k - max(past) if past else None


def build_truth(p: Patient, rng: random.Random) -> None:
    cycle_mult = [rng.uniform(0.85, 1.15) for _ in p.infusion_days]
    weight = p.baseline_weight
    for k in range(1, STUDY_DAYS + 1):
        ds = days_since_infusion(p, k)
        if ds is None:
            base = 0.3
        else:
            cyc = max(i for i, d in enumerate(p.infusion_days) if d <= k)
            base = p.peak * CURVE.get(ds, 0.05) * cycle_mult[cyc]
        if p.switch_day is not None:
            base *= 1.2 if k < p.switch_day else 0.5
        nrs = max(0, min(10, round(base + rng.gauss(0, 0.8))))

        if nrs >= 5:
            vomits = max(0, round(rng.gauss((nrs - 4) * 0.8, 0.7)))
        else:
            vomits = 1 if (nrs >= 4 and rng.random() < 0.2) else 0
        appetite = max(0, min(10, round(nrs * 0.8 + rng.gauss(0, 1))))
        if nrs >= 6:
            prn = rng.choice([1, 2, 3])
        elif nrs >= 4:
            prn = rng.choice([0, 1, 1, 2])
        elif nrs >= 2:
            prn = rng.choice([0, 0, 1])
        else:
            prn = 0
        weight -= 0.048 * nrs
        p.truth[k] = {
            "nrs": nrs, "vomits": vomits, "appetite": appetite, "prn": prn,
            "fluids": "partly" if vomits >= 4 else "yes",
            "weight": round(weight + rng.gauss(0, 0.2), 1),
        }


# ---------------------------------------------------------------------------
# Routine report rendering (labels come from the truth)
# ---------------------------------------------------------------------------

NAUSEA_ADJ = {
    1: ["a little nauseous", "mildly nauseous", "slightly queasy"],
    2: ["moderately nauseous"],
    3: ["severely nauseous", "very nauseous"],
    4: ["extremely nauseous", "unbearably nauseous"],
}
TIMES_EVENING = {   # report written the same evening about "today"
    "morning": ["this morning"],
    "afternoon": ["this afternoon", "after lunch"],
    "evening": ["after dinner", "tonight"],
    "all_day": ["all day"],
    "unknown": ["today"],
}
TIMES_NEXT_MORNING = {   # report written the next morning about "yesterday"
    "morning": ["yesterday morning"],
    "afternoon": ["yesterday afternoon"],
    "evening": ["last night", "yesterday evening"],
    "overnight": ["overnight", "during the night"],
    "all_day": ["all day yesterday"],
    "unknown": ["yesterday"],
}


def count_word(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def render_free_text(p: Patient, k: int, mode: str, rng: random.Random):
    t = p.truth[k]
    d = p.iso(k)
    times = TIMES_EVENING if mode == "evening" else TIMES_NEXT_MORNING
    day_word = times["unknown"][0]
    level = nrs_to_level(t["nrs"])
    brand = rng.choice(PRN_NAMES[p.prn_med])
    sents, syms, intakes = [], [], []

    def pick_time(allowed):
        part = rng.choice(allowed)
        return part, rng.choice(times[part])

    if level == 0 and t["vomits"] == 0:
        if mode == "evening":
            sents.append("Feeling pretty good today, no nausea or vomiting.")
        else:
            sents.append("Yesterday was a good day, no nausea or vomiting.")
        syms += [sym("nausea", "absent", d, level=0), sym("vomiting", "absent", d, level=0, cmin=0, cmax=0)]
        if t["appetite"] <= 1 and rng.random() < 0.5:
            sents.append("Appetite is okay.")
            syms.append(sym("appetite_loss", "absent", d, level=0))
        return " ".join(sents), syms, intakes

    despite = level >= 2 and t["prn"] > 0 and rng.random() < 0.35
    if level == 0:
        sents.append(f"No nausea {day_word}.")
        syms.append(sym("nausea", "absent", d, level=0))
    else:
        part, phrase = pick_time([x for x in times if x != "overnight"] if mode == "evening" else list(times))
        adj = rng.choice(NAUSEA_ADJ[level])
        if despite:
            sents.append(f"Still felt {adj} {phrase} even after taking {brand}.")
            syms.append(sym("nausea", "present", d, part, level=level,
                            related_med=p.prn_med, med_relation="despite_taking"))
        else:
            sents.append(f"Felt {adj} {phrase}.")
            syms.append(sym("nausea", "present", d, part, level=level))

    if t["vomits"] > 0:
        part, phrase = pick_time(["morning", "afternoon", "evening", "unknown"])
        verb = rng.choice(["Threw up", "Vomited"])
        sents.append(f"{verb} {count_word(t['vomits'])} {phrase}.")
        syms.append(sym("vomiting", "present", d, part, cmin=t["vomits"], cmax=t["vomits"]))
    elif rng.random() < 0.6:
        sents.append(f"No vomiting {day_word}." if mode == "next_morning" else "No vomiting.")
        syms.append(sym("vomiting", "absent", d, level=0, cmin=0, cmax=0))

    if t["appetite"] >= 5 and rng.random() < 0.5:
        sents.append("Not much appetite.")
        syms.append(sym("appetite_loss", "present", d))
    elif t["appetite"] <= 1 and rng.random() < 0.3:
        sents.append("Appetite is okay.")
        syms.append(sym("appetite_loss", "absent", d, level=0))

    if t["prn"] > 0 and (despite or rng.random() < 0.6):
        if t["prn"] == 1 and not despite and rng.random() < 0.5:
            part, phrase = pick_time(["morning", "afternoon", "evening"])
            sents.append(f"Took {brand} {phrase}.")
            intakes.append(intake(p.prn_med, "taken", d, part))
        else:
            sents.append(f"Took {brand} {count_word(t['prn'])} {day_word}.")
            intakes.append(intake(p.prn_med, "taken", d, "unknown", t["prn"]))

    text = " ".join(sents)
    if mode == "next_morning" and rng.random() < 0.3:
        text = "Update for yesterday: " + text
    return text, syms, intakes


def questionnaire_expected(p: Patient, k: int, q: dict) -> dict:
    d = p.iso(k)
    syms = []
    n = q["nausea_0_10"]
    syms.append(sym("nausea", "present" if n > 0 else "absent", d, "all_day", level=nrs_to_level(n), nrs=n))
    v = q["vomit_episodes_24h"]
    syms.append(sym("vomiting", "present" if v > 0 else "absent", d, "all_day",
                    level=None if v > 0 else 0, cmin=v, cmax=v))
    a = q["appetite_loss_0_10"]
    syms.append(sym("appetite_loss", "present" if a > 0 else "absent", d, "all_day", level=nrs_to_level(a), nrs=a))
    f = q["kept_fluids_down"]
    flags = []
    if f == "yes":
        syms.append(sym("unable_to_keep_fluids", "absent", d, "all_day", level=0))
    else:
        syms.append(sym("unable_to_keep_fluids", "present", d, "all_day"))
        if f == "no":
            flags.append("red_flag")
    intakes = []
    if q["prn_antiemetic_doses_24h"] > 0:
        intakes.append(intake(p.prn_med, "taken", d, "all_day", q["prn_antiemetic_doses_24h"]))
    weights = [{"weight_kg": q["weight_kg"], "measured_date": d}] if q.get("weight_kg") is not None else []
    return expected(syms, intakes, weights, flags)


def questionnaire_from_truth(p: Patient, k: int, with_weight: bool) -> dict:
    t = p.truth[k]
    return {
        "nausea_0_10": t["nrs"],
        "vomit_episodes_24h": t["vomits"],
        "appetite_loss_0_10": t["appetite"],
        "kept_fluids_down": t["fluids"],
        "prn_antiemetic_doses_24h": t["prn"],
        "weight_kg": t["weight"] if with_weight else None,
        "comments": None,
    }


def ts(p: Patient, k: int, hh: int, mm: int) -> str:
    return datetime.combine(p.day(k), time(hh, mm)).isoformat()


# ---------------------------------------------------------------------------
# Hand-written tricky cases (15 cases, 18 reports), labelled by hand
# ---------------------------------------------------------------------------

def build_special_cases() -> tuple[list[dict], dict[str, set[int]]]:
    P1, P2, P3, P4, P5 = (PBY[x] for x in ("P001", "P002", "P003", "P004", "P005"))
    reports: list[dict] = []
    reserved: dict[str, set[int]] = {p.patient_id: set() for p in PATIENTS}

    def add(key, p, submitted_at, source, content, exp, tags, notes, described):
        rep = {"key": key, "patient_id": p.patient_id, "submitted_at": submitted_at,
               "source": source, "expected": exp, "case_tags": tags, "notes": notes,
               "label_source": "hand_annotated"}
        if source == "free_text":
            rep["raw_text"] = content
        else:
            rep["questionnaire"] = content
        reports.append(rep)
        reserved[p.patient_id].update(described)

    # S1 vague count + overnight date anchoring
    add("S1", P1, ts(P1, 4, 8, 10), "free_text",
        "Rough night. Threw up a few times after midnight, lost count honestly. "
        "Still a bit queasy this morning.",
        expected([sym("vomiting", "present", P1.iso(3), "overnight", cmin=2, cmax=4),
                  sym("nausea", "present", P1.iso(4), "morning", level=1)]),
        ["vague_count", "relative_time"],
        "'a few times' -> 2-4. 'After midnight' in a morning report = the night that started "
        "the previous day (convention). 'a bit queasy' -> mild.", {3, 4})

    # S2 uncertain presence
    add("S2", P3, ts(P3, 8, 20, 40), "free_text",
        "Felt kind of off today, not sure if it's nausea or just tired. Ate okay.",
        expected([sym("nausea", "uncertain", P3.iso(8)),
                  sym("appetite_loss", "absent", P3.iso(8), level=0)]),
        ["vague_presence"],
        "Patient is unsure -> presence 'uncertain', severity NULL. Must not become mild nausea.", {8})

    # S3 negation with hedge
    add("S3", P4, ts(P4, 20, 19, 5), "free_text",
        "No throwing up today. Nausea pretty much gone, maybe a tiny bit after breakfast.",
        expected([sym("vomiting", "absent", P4.iso(20), level=0, cmin=0, cmax=0),
                  sym("nausea", "present", P4.iso(20), "morning", level=1)]),
        ["negation", "hedged_severity"],
        "'pretty much gone, maybe a tiny bit' -> present, mild, morning.", {20})

    # S4 questionnaire vs. its own comment
    add("S4", P5, ts(P5, 6, 20, 30), "questionnaire",
        {"nausea_0_10": 5, "vomit_episodes_24h": 0, "appetite_loss_0_10": 6,
         "kept_fluids_down": "yes", "prn_antiemetic_doses_24h": 1, "weight_kg": None,
         "comments": "Threw up once after lunch, forgot to count it above, sorry."},
        expected([sym("nausea", "present", P5.iso(6), "all_day", level=2, nrs=5),
                  sym("vomiting", "absent", P5.iso(6), "all_day", level=0, cmin=0, cmax=0),
                  sym("vomiting", "present", P5.iso(6), "afternoon", cmin=1, cmax=1),
                  sym("appetite_loss", "present", P5.iso(6), "all_day", level=2, nrs=6),
                  sym("unable_to_keep_fluids", "absent", P5.iso(6), "all_day", level=0)],
                 [intake("ondansetron", "taken", P5.iso(6), "all_day", 1)],
                 flags=["intra_report_conflict"]),
        ["intra_report_conflict"],
        "Keep BOTH vomiting rows (field says 0, comment says 1) and flag. Daily view shows 1.", {6})

    # S5 two reports, same day, disagree
    add("S5a", P1, ts(P1, 24, 19, 30), "free_text",
        "Vomited 3 times this afternoon, could barely get off the couch.",
        expected([sym("vomiting", "present", P1.iso(24), "afternoon", cmin=3, cmax=3)]),
        ["cross_report_conflict"], "First half of a same-day conflict (see S5b).", {24})
    add("S5b", P1, ts(P1, 24, 21, 0), "questionnaire",
        {"nausea_0_10": 7, "vomit_episodes_24h": 1, "appetite_loss_0_10": 8,
         "kept_fluids_down": "yes", "prn_antiemetic_doses_24h": 2, "weight_kg": None, "comments": None},
        expected([sym("nausea", "present", P1.iso(24), "all_day", level=3, nrs=7),
                  sym("vomiting", "present", P1.iso(24), "all_day", cmin=1, cmax=1),
                  sym("appetite_loss", "present", P1.iso(24), "all_day", level=3, nrs=8),
                  sym("unable_to_keep_fluids", "absent", P1.iso(24), "all_day", level=0)],
                 [intake("ondansetron", "taken", P1.iso(24), "all_day", 2)],
                 cross_flags=["cross_report_conflict"]),
        ["cross_report_conflict"],
        "Questionnaire says 1 vomit in 24h, earlier text said 3 this afternoon. "
        "Daily view: max(1, 3) = 3 with has_open_conflict.", {24})

    # S6 correction next morning
    add("S6a", P2, ts(P2, 3, 20, 15), "free_text",
        "Threw up once today after dinner, nausea moderate.",
        expected([sym("vomiting", "present", P2.iso(3), "evening", cmin=1, cmax=1),
                  sym("nausea", "present", P2.iso(3), "unknown", level=2)]),
        ["correction_target"], "Will be corrected by S6b.", {3})
    add("S6b", P2, ts(P2, 4, 8, 30), "free_text",
        "Correction to yesterday: it was actually 3 times, not once. I was too wiped out to remember.",
        expected([sym("vomiting", "present", P2.iso(3), "unknown", cmin=3, cmax=3)],
                 supersedes="S6a"),
        ["correction"],
        "Supersedes the vomiting event from S6a (old row -> status 'superseded', not deleted).", set())

    # S7 severity word contradicts function + red flag
    add("S7", P5, ts(P5, 25, 20, 0), "free_text",
        "Nausea is mild I guess, but I haven't been able to keep any water down since this morning. "
        "Threw up 4 times.",
        expected([sym("nausea", "present", P5.iso(25), "unknown", level=1),
                  sym("unable_to_keep_fluids", "present", P5.iso(25), "all_day"),
                  sym("vomiting", "present", P5.iso(25), "unknown", cmin=4, cmax=4)],
                 flags=["severity_mismatch", "red_flag"]),
        ["severity_mismatch", "red_flag"],
        "Store 'mild' as said, but flag the mismatch; can't keep fluids down is surfaced as a red flag "
        "(dashboard prominence, not a diagnosis).", {25})

    # S8 medication that can't be mapped confidently
    add("S8", P3, ts(P3, 16, 19, 45), "free_text",
        "Took the little pill that melts on my tongue around 2pm, nausea eased after an hour.",
        expected([sym("nausea", "present", P3.iso(16), "afternoon")],
                 [intake(None, "taken", P3.iso(16), "afternoon")],
                 flags=["unmapped_medication"]),
        ["unmapped_medication"],
        "Likely ondansetron ODT, but the parser must not guess: med NULL + flag for review. "
        "Nausea present, no intensity given.", {16})

    # S9 implausible number
    add("S9", P1, ts(P1, 23, 20, 50), "free_text",
        "Threw up like a hundred times today, it just would not stop.",
        expected([sym("vomiting", "present", P1.iso(23), "unknown", cmin=1, cmax=None)],
                 flags=["implausible_value"]),
        ["implausible_value"],
        "Hyperbole: do not store 100. At least 1, upper bound unknown, flag for follow-up.", {23})

    # S10 vomited up the dose
    add("S10", P2, ts(P2, 2, 11, 0), "free_text",
        "Took my ondansetron at 8 this morning but threw it up about 20 minutes later. "
        "Took another at 10 and that one stayed down.",
        expected([sym("vomiting", "present", P2.iso(2), "morning", cmin=1, cmax=1,
                      related_med="ondansetron", med_relation="vomited_up_dose")],
                 [intake("ondansetron", "vomited_up", P2.iso(2), "morning"),
                  intake("ondansetron", "taken", P2.iso(2), "morning")]),
        ["vomited_up_dose"], "Two intake rows: one vomited up, one taken.", {2})

    # S11 symptom despite medication
    add("S11", P5, ts(P5, 4, 19, 20), "free_text",
        "Still really nauseous even after taking Zofran at lunch. No vomiting.",
        expected([sym("nausea", "present", P5.iso(4), "afternoon", level=3,
                      related_med="ondansetron", med_relation="despite_taking"),
                  sym("vomiting", "absent", P5.iso(4), level=0, cmin=0, cmax=0)],
                 [intake("ondansetron", "taken", P5.iso(4), "afternoon")]),
        ["despite_medication", "brand_name"], "'really nauseous' -> severe (3). Zofran -> ondansetron.", {4})

    # S12 relative date two days back
    add("S12", P3, ts(P3, 18, 20, 10), "free_text",
        "Day before yesterday was the worst, vomited 4 times. Today just mild nausea.",
        expected([sym("vomiting", "present", P3.iso(16), "unknown", cmin=4, cmax=4),
                  sym("nausea", "present", P3.iso(18), "unknown", level=1)]),
        ["relative_time"], "Vomiting belongs to day-2 relative to submission.", {16, 18})

    # S13 patient-reported weight in free text
    add("S13", P1, ts(P1, 15, 8, 5), "free_text",
        "Weighed myself this morning: 72.1 kg. Appetite still poor.",
        expected([sym("appetite_loss", "present", P1.iso(15))],
                 weights=[{"weight_kg": 72.1, "measured_date": P1.iso(15)}]),
        ["weight_in_text"], "Weight goes to weight_measurements (patient_reported).", {15})

    # S14 missed dose after medication change
    add("S14", P2, ts(P2, 13, 9, 0), "free_text",
        "Forgot my olanzapine last night. Nausea is worse this morning.",
        expected([sym("nausea", "present", P2.iso(13), "morning")],
                 [intake("olanzapine", "missed", P2.iso(12), "evening")]),
        ["missed_dose", "relative_severity"],
        "'worse' is relative, not a level -> severity NULL. Missed dose dated the previous evening.", {12, 13})

    # S15 duplicate submission
    dup_text = "Mild nausea after lunch, no vomiting."
    dup_exp = lambda cross: expected(
        [sym("nausea", "present", P4.iso(30), "afternoon", level=1),
         sym("vomiting", "absent", P4.iso(30), level=0, cmin=0, cmax=0)], cross_flags=cross)
    add("S15a", P4, ts(P4, 30, 14, 2), "free_text", dup_text, dup_exp([]),
        ["duplicate_original"], "Original of a double submission.", {30})
    add("S15b", P4, ts(P4, 30, 14, 3), "free_text", dup_text, dup_exp(["duplicate_report"]),
        ["duplicate_report"],
        "Same text one minute later. Flag as duplicate; daily view max-rule also prevents double counting.", {30})

    return reports, reserved


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_routine_reports(rng: random.Random, reserved: dict[str, set[int]]) -> list[dict]:
    reports = []
    for p in PATIENTS:
        for k in range(1, STUDY_DAYS + 1):
            if k in reserved[p.patient_id]:
                continue
            weekly = k % 7 == 0
            if weekly or rng.random() < 0.15:
                q = questionnaire_from_truth(p, k, with_weight=weekly)
                reports.append({
                    "patient_id": p.patient_id,
                    "submitted_at": ts(p, k, rng.randint(19, 21), rng.randint(0, 59)),
                    "source": "questionnaire", "questionnaire": q,
                    "expected": questionnaire_expected(p, k, q),
                    "case_tags": ["routine"], "notes": None, "label_source": "generator",
                })
            elif rng.random() < p.report_rate:
                mode = "next_morning" if (k < STUDY_DAYS and rng.random() < 0.2) else "evening"
                text, syms, intakes = render_free_text(p, k, mode, rng)
                sub = ts(p, k + 1, rng.randint(7, 10), rng.randint(0, 59)) if mode == "next_morning" \
                    else ts(p, k, rng.randint(18, 22), rng.randint(0, 59))
                reports.append({
                    "patient_id": p.patient_id, "submitted_at": sub,
                    "source": "free_text", "raw_text": text,
                    "expected": expected(syms, intakes),
                    "case_tags": ["routine", mode], "notes": None, "label_source": "generator",
                })
    return reports


def build_orders() -> list[dict]:
    orders = []
    for p in PATIENTS:
        start = p.iso(1)
        if p.risk in ("high", "moderate"):
            orders.append(dict(patient_id=p.patient_id, med="dexamethasone", dose="8 mg", route="PO",
                               frequency="once daily on days 2-3 after each infusion", is_prn=0,
                               start_date=start, end_date=None, change_reason=None))
        if p.prn_med == "ondansetron":
            orders.append(dict(patient_id=p.patient_id, med="ondansetron", dose="8 mg", route="PO",
                               frequency="every 8 hours as needed for nausea", is_prn=1,
                               start_date=start, end_date=None, change_reason=None))
        else:
            orders.append(dict(patient_id=p.patient_id, med="prochlorperazine", dose="10 mg", route="PO",
                               frequency="every 6 hours as needed for nausea", is_prn=1,
                               start_date=start, end_date=None, change_reason=None))
        if p.switch_day:
            orders.append(dict(patient_id=p.patient_id, med="olanzapine", dose="5 mg", route="PO",
                               frequency="nightly", is_prn=0, start_date=p.iso(p.switch_day),
                               end_date=None,
                               change_reason="Delayed nausea not controlled on PRN ondansetron + "
                                             "dexamethasone in cycle 1 (synthetic scenario)"))
    return orders


def main() -> None:
    rng = random.Random(SEED)
    for p in PATIENTS:
        build_truth(p, rng)

    specials, reserved = build_special_cases()
    routine = build_routine_reports(rng, reserved)
    reports = sorted(specials + routine, key=lambda r: (r["submitted_at"], r["patient_id"]))
    key_to_id = {}
    for i, r in enumerate(reports, start=1):
        r["report_id"] = f"R{i:04d}"
        if "key" in r:
            key_to_id[r["key"]] = r["report_id"]
    for r in reports:
        sk = r["expected"].pop("supersedes_report_key")
        r["expected"]["supersedes_report_id"] = key_to_id[sk] if sk else None

    dataset = {
        "meta": {"description": "Synthetic CINV patient-report dataset for the Dr.Tracker Track B "
                                "prototype. Fictional patients; simplified regimens; not clinical guidance.",
                 "seed": SEED, "study_days": STUDY_DAYS},
        "medications": [dict(generic_name=g, brand_names=b, drug_class=c, is_antiemetic=a)
                        for g, b, c, a in MEDICATIONS],
        "patients": [dict(patient_id=p.patient_id, display_name=p.display_name, diagnosis=p.diagnosis,
                          regimen_name=p.regimen_name, emetogenic_risk=p.risk, enrolled_at=p.iso(1),
                          baseline_weight_kg=p.baseline_weight, weight_alert_pct=5.0)
                     for p in PATIENTS],
        "chemo_administrations": [dict(patient_id=p.patient_id, cycle_number=i + 1,
                                       administered_at=ts(p, d, 9, 30), agents=p.agents)
                                  for p in PATIENTS for i, d in enumerate(p.infusion_days)],
        "medication_orders": build_orders(),
        "weight_measurements": [dict(patient_id=p.patient_id, measured_date=p.iso(d),
                                     weight_kg=p.truth[d]["weight"], source="clinic")
                                for p in PATIENTS for d in p.infusion_days],
        "raw_reports": [{"report_id": r["report_id"], "patient_id": r["patient_id"],
                         "submitted_at": r["submitted_at"], "source": r["source"],
                         "raw_text": r.get("raw_text"), "questionnaire": r.get("questionnaire")}
                        for r in reports],
    }

    # Gold set: all hand-written cases + a seeded sample of routine reports.
    special_reports = [r for r in reports if r["label_source"] == "hand_annotated"]
    routine_ft = [r for r in reports if r["label_source"] == "generator" and r["source"] == "free_text"]
    routine_q = [r for r in reports if r["label_source"] == "generator" and r["source"] == "questionnaire"]
    gold_rng = random.Random(SEED + 1)
    n_routine = 30 - len(special_reports)
    n_q = 5
    # Stratify so easy "good day" reports don't dominate and inflate accuracy.
    def symptomatic(r):
        return any(s["presence"] == "present" for s in r["expected"]["symptom_events"])
    ft_sym = [r for r in routine_ft if symptomatic(r)]
    ft_quiet = [r for r in routine_ft if not symptomatic(r)]
    q_sym = [r for r in routine_q if symptomatic(r)]
    q_quiet = [r for r in routine_q if not symptomatic(r)]
    n_ft = n_routine - n_q
    sample = (gold_rng.sample(ft_sym, n_ft - 2) + gold_rng.sample(ft_quiet, 2)
              + gold_rng.sample(q_sym, n_q - 1) + gold_rng.sample(q_quiet, 1))
    gold_reports = sorted(special_reports + sample, key=lambda r: r["report_id"])
    gold = {
        "meta": {
            "description": "Expected parser output for 30 reports. 'hand_annotated' labels were written "
                           "case by case; 'generator' labels come from the synthetic truth used to "
                           "render the text.",
            "conventions": CONVENTIONS,
            "scored_symptom_fields": ["symptom_code", "presence", "severity_level", "severity_nrs",
                                      "episode_count_min", "episode_count_max", "occurred_date",
                                      "part_of_day", "related_med", "med_relation"],
        },
        "reports": [{
            "report_id": r["report_id"], "patient_id": r["patient_id"],
            "submitted_at": r["submitted_at"], "source": r["source"],
            "input": r.get("raw_text") if r["source"] == "free_text" else r.get("questionnaire"),
            "label_source": r["label_source"], "case_tags": r["case_tags"], "notes": r["notes"],
            "expected": r["expected"],
        } for r in gold_reports],
    }

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "synthetic_dataset.json").write_text(json.dumps(dataset, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
    (OUT_DIR / "gold_set.json").write_text(json.dumps(gold, indent=2, ensure_ascii=False), encoding="utf-8")

    n_ft = sum(r["source"] == "free_text" for r in reports)
    print(f"reports: {len(reports)} (free_text {n_ft}, questionnaire {len(reports) - n_ft})")
    print(f"hand-written tricky reports: {len(special_reports)}; gold set: {len(gold_reports)}")
    for p in PATIENTS:
        n = sum(r["patient_id"] == p.patient_id for r in reports)
        print(f"  {p.patient_id}: {n} reports, infusion days {p.infusion_days}")


if __name__ == "__main__":
    main()
