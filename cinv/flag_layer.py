"""Deterministic flag layer applied after the LLM (prompt unchanged).

The LLM output itself is never modified here: this layer only *adds* review
flags, from (a) the LLM's own rows (uncertain presence), (b) a second opinion
from the keyword-rule parser, and (c) regex checks on the patient's text.

Rules were written after looking at the LLM's errors on the 88-report messy set,
so that set is now a development set for this layer, not a blind test.

Flags raised (one flag per type per report; the reasons are listed in the description):
  low_confidence       uncertain presence, hedging, empty / emoji-only / non-English text,
                       second-hand account, uncertain date
  parser_disagreement  rule parser and LLM disagree on symptoms, presence or counts
  distress             emotional-distress wording -> urgent, handled like a red flag
  other_symptom        complaint outside the 5 schema symptoms (fever also raises red_flag)
  prompt_injection     instruction-like or JSON-like text aimed at the parser
"""

from __future__ import annotations

import json
import re
import unicodedata

from .common import Context, postprocess
from .models import COUNTED, FlagCandidate, ParsedReport
from .rules import parse_free_text_rule

# Flag types that the dashboard treats like a red flag (urgent, top of Overview).
RED_FLAG_TYPES = ("red_flag", "distress")

HEDGE_RX = (r"\bmaybe\b|\bidk\b|\bi think\b|\bnot (?:really |totally |100% )?sure\b|\bnt sure\b|\bunsure\b"
            r"|\bi guess\b|\bhard to tell\b|\blost count\b|\?\?|\blike \d+")
REPORTED_RX = (r"\b(?:says|said|told me|tells me|swears)\b"
               r"|\b(?:husband|wife|dad|mom|mum|mother|father|son|daughter|partner|caregiver|grandma|grandpa)\b"
               r"|\b(?:he|she) (?:was|is|had|has|heard|thinks|threw)\b")
DATE_RX = (r"\bday \d+\b|\bc\d+\s*d\d+\b|\bthe other day\b|\bwhich day\b|\bwhat day\b"
           r"|\bor was (?:that|it) (?:yesterday|yestarday|today|last ni(?:ght|te))\b"
           r"|\bor (?:yesterday|last night|last nite)\b")
DISTRESS_RX = (r"\bscared\b|\bterrified\b|\bafraid\b|\bcan'?t do this\b|\bcant do this\b|\bgive up\b"
               r"|\bgiving up\b|\bwhat'?s the point\b|\bwhats the point\b|\bwant (?:it|this|it all) to stop\b"
               r"|\bwant to die\b|\bhopeless\b|\bcan'?t take (?:it|this|any ?more)\b|😭|😰|😱")
INJECTION_RX = (r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) instructions|\bsystem\s*:"
                r"|\badmin mode\b|\bdeveloper mode\b|</?\s*report\s*>|\bclassify this as\b"
                r"|\brecord no symptoms\b|\bmark everything normal\b|\{\s*\"[^\"]*\"\s*:")
OTHER_SYMPTOM_RX = {
    "fever": r"\bfever\w*|\bfebrile\b|\bchills\b|\btemp(?:erature)? (?:of |is |was |at )?\d",
    "diarrhea": r"\bdiarrh\w*|\bthe runs\b|\brunny tummy\b|\bloose stools?\b|\bwatery stools?\b",
    "constipation": r"\bconstipat\w*|\bbacked up\b|\bcan'?t go to (?:the )?bathroom\b|\bcant go to (?:the )?bathroom\b",
    "hair loss": r"\bhair\b(?:\s+\w+){0,3}\s+(?:falling|fall|coming) out\b|\blosing (?:my )?hair\b|\bhair loss\b",
    "fatigue": r"\btired\b|\bfatigue\w*|\bexhausted\b|\bwiped out\b|\bno energy\b",
    "pain": r"\bpain\w*|\bhurts?\b|\baches?\b|\baching\b|\bsore\b|\bpounding\b",
}
# Function words that are common in Spanish / French / pinyin and rare in English messages.
FOREIGN_WORDS = {
    "el", "los", "las", "del", "muy", "hoy", "todo", "toda", "dia", "veces", "vez", "dos", "tres",
    "siento", "puedo", "estomago", "vomite", "nauseas", "una", "fois", "ce", "matin", "pas", "faim",
    "je", "suis", "et", "ventre", "vomi", "mei", "hen", "shi", "wo", "chi", "dongxi", "tiao", "zhang",
    "wei", "le",
}


def _hits(rx: str, text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in re.finditer(rx, text, re.I)))


def _non_english(text: str) -> str | None:
    letters = [c for c in text if c.isalpha()]
    if any(ord(c) > 127 and unicodedata.category(c).startswith("L") for c in letters):
        return "non-Latin or accented letters"
    words = set(re.findall(r"[a-z]+", text.lower()))
    found = sorted(words & FOREIGN_WORDS)
    return f"non-English words ({', '.join(found)})" if len(found) >= 2 else None


def _summary(events) -> dict[str, tuple[frozenset, frozenset]]:
    """symptom -> (presences, counts of present rows) for the free-text rows."""
    out: dict[str, tuple[set, set]] = {}
    for e in events:
        pres, counts = out.setdefault(e.symptom_code, (set(), set()))
        pres.add(e.presence)
        if e.symptom_code in COUNTED and e.presence == "present":
            counts.add((e.episode_count_min, e.episode_count_max))
    return {k: (frozenset(p), frozenset(c)) for k, (p, c) in out.items()}


def parser_disagreement(llm_rep: ParsedReport, rule_rep: ParsedReport) -> list[str]:
    """Differences between the LLM's free-text rows and the keyword rules' rows."""
    a = _summary(e for e in llm_rep.symptom_events if e.extraction_method == "llm")
    b = _summary(rule_rep.symptom_events)
    out = []
    for code in sorted(set(a) | set(b)):
        if code not in b:
            out.append(f"{code}: LLM only")
        elif code not in a:
            out.append(f"{code}: rules only")
        elif a[code][0] != b[code][0]:
            out.append(f"{code}: presence LLM {'/'.join(sorted(a[code][0]))} vs rules {'/'.join(sorted(b[code][0]))}")
        elif a[code][1] != b[code][1]:
            fmt = lambda s: "/".join(f"{lo}-{hi}" for lo, hi in sorted(s, key=str))   # noqa: E731
            out.append(f"{code}: count LLM {fmt(a[code][1])} vs rules {fmt(b[code][1])}")
    return out


def deterministic_flags(rep: ParsedReport, text: str, rule_rep: ParsedReport | None) -> list[FlagCandidate]:
    """Extra flags for one LLM-parsed report. `text` is the free text the LLM read (the comment for a
    questionnaire); `rule_rep` is the keyword-rule parse of the same text (None = skip the comparison)."""
    rid, out = rep.report_id, []
    llm_idx = [i for i, e in enumerate(rep.symptom_events) if e.extraction_method == "llm"]
    all_refs = [(rid, "symptom", i) for i in llm_idx]
    t = text or ""

    reasons, refs = [], []
    unc = [i for i in llm_idx if rep.symptom_events[i].presence == "uncertain"]
    if unc:
        reasons.append("presence uncertain (" + ", ".join(rep.symptom_events[i].symptom_code for i in unc) + ")")
        refs += [(rid, "symptom", i) for i in unc]
    if not t.strip():
        reasons.append("empty message")
    elif not any(c.isalnum() for c in t):
        reasons.append("emoji / symbols only, no words")
    else:
        if (why := _non_english(t)):
            reasons.append(why)
        for label, rx in (("hedging", HEDGE_RX), ("second-hand account", REPORTED_RX), ("uncertain date", DATE_RX)):
            if (h := _hits(rx, t)):
                reasons.append(f"{label} (" + ", ".join(repr(x) for x in h[:4]) + ")")
                refs = all_refs
    if reasons:
        out.append(FlagCandidate(flag_type="low_confidence", severity="warning", report_id=rid,
                                 description="Deterministic check: " + "; ".join(reasons) + ".",
                                 refs=list(dict.fromkeys(refs))))

    if rule_rep is not None and (diff := parser_disagreement(rep, rule_rep)):
        out.append(FlagCandidate(flag_type="parser_disagreement", severity="warning", report_id=rid,
                                 description="Rule parser and LLM disagree: " + "; ".join(diff) + ".",
                                 refs=all_refs))

    if (h := _hits(DISTRESS_RX, t)):
        out.append(FlagCandidate(flag_type="distress", severity="urgent", report_id=rid,
                                 description="Distress wording: " + ", ".join(repr(x) for x in h) +
                                             ". Not a symptom; needs a person to reach out."))

    found = {name: h for name, rx in OTHER_SYMPTOM_RX.items() if (h := _hits(rx, t))}
    if found:
        out.append(FlagCandidate(flag_type="other_symptom", severity="warning", report_id=rid,
                                 description="Complaint outside the symptom schema: " + "; ".join(
                                     f"{k} ({', '.join(repr(x) for x in v)})" for k, v in found.items()) + "."))
    if "fever" in found:
        out.append(FlagCandidate(flag_type="red_flag", severity="urgent", report_id=rid,
                                 description="Patient mentions fever: " +
                                             ", ".join(repr(x) for x in found["fever"]) + "."))

    if (h := _hits(INJECTION_RX, t)):
        out.append(FlagCandidate(flag_type="prompt_injection", severity="warning", report_id=rid,
                                 description="Instruction-like text aimed at the parser: " +
                                             ", ".join(repr(x[:40]) for x in h) + ". Verify every row.",
                                 refs=all_refs))
    return out


def free_text_of(row: dict) -> str:
    if row["source"] == "free_text":
        return row["raw_text"] or ""
    q = row["questionnaire_json"]
    return ((json.loads(q) if isinstance(q, str) else q).get("comments") or "").strip()


def apply_flag_layer(rep: ParsedReport, row: dict, ctx: Context) -> list[FlagCandidate]:
    """Run the layer for a report whose free text was parsed by the LLM; otherwise no flags.
    A questionnaire without a comment has no LLM output, so nothing is checked."""
    if rep.method != "llm" or rep.failed:
        return []
    text = free_text_of(row)
    if row["source"] == "questionnaire" and not text:
        return []
    rule_rep = postprocess(parse_free_text_rule(rep.report_id, rep.patient_id, text, row["submitted_at"], ctx))
    return deterministic_flags(rep, text, rule_rep)
