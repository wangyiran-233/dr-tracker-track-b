"""Visual theme: one teal palette (primary #0F766E and its tints / shades). Red is reserved
for red flags and vomiting, amber for weight alerts. Injected once per run from app.py."""

from __future__ import annotations

import base64
import html

import streamlit as st

TEAL = "#0F766E"
TEAL_DARK = "#134E4A"
TEAL_MID = "#14B8A6"
TEAL_TINT = "#E6F4F1"
TEAL_LINE = "#B7DED6"
RED = "#B42318"
RED_TINT = "#FEF3F2"
RED_LINE = "#F5C2BC"
AMBER = "#B45309"
AMBER_TINT = "#FFFBEB"
AMBER_LINE = "#F6D58E"
SLATE = "#475569"
INK = "#0F172A"
MUTED = "#64748B"
BORDER = "#E2E8F0"
MONO = "ui-monospace, 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace"

_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' width='22' height='22' viewBox='0 0 24 24' fill='none' "
        "stroke='white' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>"
        "<polyline points='3 12 7 12 10 5 14 19 17 12 21 12'/></svg>")
PULSE_SVG = ("<img alt='' width='22' height='22' src='data:image/svg+xml;base64,"
             + base64.b64encode(_SVG.encode()).decode() + "'>")

CSS = f"""
<style>
:root {{ --teal:{TEAL}; --border:{BORDER}; --muted:{MUTED}; --mono:{MONO}; }}
.stApp {{ background:#F8FAFB; }}
[data-testid="stHeader"] {{ background:transparent; }}
.block-container, [data-testid="stMainBlockContainer"] {{ padding-top:2.2rem; padding-bottom:3rem; max-width:1500px; }}
h1 {{ font-size:2.05rem !important; font-weight:750 !important; letter-spacing:-.02em; color:{INK};
      padding:.1rem 0 .15rem !important; }}

/* ---------- cards: every keyed container whose key starts with card_ */
[class*="st-key-card_"] {{ background:#fff; border:1px solid {BORDER}; border-radius:14px;
      padding:20px 24px 18px; box-shadow:0 1px 2px rgba(15,23,42,.04); gap:.65rem; }}
[class*="st-key-card_dev"] {{ background:{AMBER_TINT}; border-color:{AMBER_LINE}; }}

/* ---------- Needs Attention cards: equal height, footer pinned to the bottom */
[class*="st-key-attncard"] {{ border:1px solid {BORDER}; border-radius:12px; padding:16px 18px 14px;
      min-height:270px; flex:1; justify-content:space-between; background:#fff; }}
[class*="st-key-attncard-red"] {{ background:#FFFAFA; border-color:{RED_LINE}; }}
[class*="st-key-attncard-weight"] {{ background:#FFFDF5; border-color:{AMBER_LINE}; }}
[class*="st-key-attncard-silent"] {{ background:#FCFCFD; border-color:#D0D5DD; }}
[data-testid="stColumn"] > [data-testid="stVerticalBlock"]:has([class*="st-key-attncard"]) {{ height:100%; }}
[data-testid="stLayoutWrapper"]:has(> [class*="st-key-attncard"]) {{ flex:1; }}

/* ---------- cohort table rows: whole row clickable via a transparent overlay button */
[class*="st-key-cohortrow"] {{ position:relative; border-top:1px solid #EEF2F4; padding:12px 6px;
      border-radius:8px; transition:background .12s; }}
[class*="st-key-cohortrow"]:hover {{ background:#F3FAF8; }}
[class*="st-key-cohortrow"] .stButton {{ position:absolute; inset:0; z-index:5; margin:0; }}
[class*="st-key-cohortrow"] .stButton button {{ width:100%; height:100%; opacity:0; cursor:pointer; }}
.st-key-cohorthead {{ padding:6px 6px 2px; }}

/* ---------- primary buttons in teal; tertiary as links */
button[kind="primary"], [data-testid="stBaseButton-primary"] {{ background:{TEAL}; border-color:{TEAL};
      font-weight:600; border-radius:8px; }}
button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {{ background:{TEAL_DARK};
      border-color:{TEAL_DARK}; }}
[data-testid="stBaseButton-tertiary"] {{ color:{TEAL}; padding:0; min-height:0; font-weight:500; }}

/* ---------- pills / segmented controls: teal when selected */
button[data-variant="pills"] {{ border-radius:999px; }}
button[data-variant][data-selected="true"] {{ background:{TEAL} !important; color:#fff !important;
      border-color:{TEAL} !important; }}
button[data-variant][data-selected="true"] p {{ color:#fff !important; font-weight:600; }}
button[data-variant="segmented_control"][data-selected="true"] {{ background:#fff !important;
      color:{TEAL} !important; border-color:{TEAL_LINE} !important; }}
button[data-variant="segmented_control"][data-selected="true"] p {{ color:{TEAL} !important; }}

/* ---------- sidebar */
[data-testid="stSidebar"] {{ background:#F4F7F8; border-right:1px solid {BORDER}; }}
[data-testid="stSidebarUserContent"] {{ padding-top:1.2rem; }}
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] {{ min-height:calc(100vh - 8rem); }}
[data-testid="stLayoutWrapper"]:has(> .st-key-devbox) {{ margin-top:auto; }}
.st-key-devbox {{ border-top:1px solid {BORDER}; padding-top:14px; }}
.st-key-devcard {{ background:#fff; border:1px solid {BORDER}; border-radius:12px; padding:12px 14px; gap:.2rem; }}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] {{ border-radius:10px; padding:.45rem .7rem;
      border:1px solid transparent; }}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"]:has(span[aria-current]),
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][aria-current="page"] {{ background:#fff;
      border-color:{BORDER}; box-shadow:0 1px 2px rgba(15,23,42,.05); }}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] p {{ font-size:.95rem; white-space:nowrap; }}
[class*="st-key-nav_"] a {{ display:flex; align-items:center; }}
[class*="st-key-nav_"] a::after {{ margin-left:auto; font-family:{MONO}; font-size:.72rem; font-weight:600;
      padding:1px 7px; border-radius:6px; border:1px solid {BORDER}; background:#F1F5F9; color:{SLATE}; }}
.st-key-nav_overview a::after {{ background:{RED_TINT}; color:{RED}; border-color:{RED_LINE}; }}
.st-key-nav_inspector p {{ font-size:.86rem !important; }}
.st-key-nav_inspector a::after {{ content:"DEV"; background:{AMBER_TINT}; color:{AMBER}; border-color:{AMBER_LINE}; }}

/* ---------- small pieces */
.dt-eyebrow {{ display:inline-flex; align-items:center; gap:7px; padding:3px 12px; border-radius:999px;
      background:{TEAL_TINT}; border:1px solid {TEAL_LINE}; color:{TEAL}; font-size:.85rem; font-weight:500; }}
.dt-eyebrow::before {{ content:""; width:7px; height:7px; border-radius:50%; background:{TEAL}; }}
.dt-sub {{ color:{MUTED}; font-size:1.02rem; margin-top:-.35rem; }}
.dt-rule {{ border:none; border-top:1px solid {BORDER}; margin:.6rem 0 .2rem; }}
.dt-tag {{ display:inline-block; border-radius:6px; padding:2px 8px; font-size:.7rem; font-weight:700;
      letter-spacing:.05em; white-space:nowrap; font-family:{MONO}; border:1px solid; }}
.dt-chip {{ display:inline-block; font-family:{MONO}; font-size:.8rem; color:{TEAL}; background:{TEAL_TINT};
      border:1px solid {TEAL_LINE}; border-radius:6px; padding:1px 7px; }}
.dt-quote {{ font-family:{MONO}; font-size:.82rem; line-height:1.55; background:#fff; border:1px solid {BORDER};
      border-radius:8px; padding:9px 12px; color:#1E293B; }}
.dt-th {{ font-size:.7rem; letter-spacing:.06em; text-transform:uppercase; color:{SLATE}; font-weight:600; }}
.dt-small {{ font-size:.8rem; color:{MUTED}; }}
.dt-mono {{ font-family:{MONO}; }}
.dt-kpis {{ display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:16px; margin:.3rem 0 .4rem; }}
@media (max-width: 1100px) {{ .dt-kpis {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }} }}
.dt-kpi {{ background:#fff; border:1px solid {BORDER}; border-radius:12px; padding:16px 18px; }}
.dt-kpi .l {{ font-size:.85rem; color:{SLATE}; }}
.dt-kpi .v {{ font-family:{MONO}; font-size:1.18rem; font-weight:700; margin:.3rem 0 .25rem; line-height:1.35; }}
.dt-kpi .s {{ font-size:.8rem; color:{MUTED}; line-height:1.45; }}
.dt-pill {{ display:inline-flex; align-items:center; gap:7px; font-family:{MONO}; font-size:.74rem; color:{TEAL};
      background:#F0FAF8; border:1px solid {TEAL_LINE}; border-radius:999px; padding:4px 12px; }}
.dt-pill::before {{ content:""; width:7px; height:7px; border-radius:50%; background:{TEAL}; }}
.dt-alert {{ border:1px solid {RED_LINE}; background:{RED_TINT}; border-radius:10px; padding:10px 14px;
      color:{RED}; font-size:.9rem; }}
.dt-label {{ font-size:.72rem; letter-spacing:.07em; text-transform:uppercase; color:{MUTED}; font-weight:600; }}
/* ---------- Nurse Review Flags (Deep-Dive) */
[class*="st-key-flagcard"] {{ background:{AMBER_TINT}; border:1px solid {AMBER_LINE}; border-radius:12px;
      padding:14px 18px; }}

/* ---------- Inspector: edge cases + benchmark */
.dt-rationale {{ background:#ECFDF5; border:1px solid #A7F3D0; border-radius:10px; padding:12px 14px;
      font-size:.88rem; line-height:1.6; color:#134E4A; margin-top:.4rem; }}
.dt-rationale b {{ color:{TEAL}; }}
.dt-code {{ background:#0F172A; border-radius:14px; padding:18px 20px; color:#E2E8F0; height:100%; }}
.dt-code .hd {{ display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap;
      font-size:.8rem; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:#5EEAD4;
      margin-bottom:10px; }}
.dt-code pre {{ margin:0; font-family:{MONO}; font-size:.78rem; line-height:1.55; white-space:pre-wrap;
      word-break:break-word; max-height:520px; overflow:auto; background:transparent; color:#E2E8F0; }}
.dt-code .k {{ color:#99F6E4; }} .dt-code .s {{ color:#FDE68A; }} .dt-code .n {{ color:#F9A8D4; }}
.dt-okbadge {{ font-family:{MONO}; font-size:.68rem; letter-spacing:.04em; padding:2px 8px; border-radius:6px;
      background:#064E3B; color:#6EE7B7; border:1px solid #10B981; text-transform:uppercase; }}
.dt-okbadge.bad {{ background:#451A03; color:#FCD34D; border-color:#D97706; text-transform:none; }}
.dt-bench {{ width:100%; border-collapse:collapse; margin-top:.6rem; font-size:.88rem; }}
.dt-bench th {{ text-align:left; font-size:.7rem; letter-spacing:.06em; text-transform:uppercase; color:{SLATE};
      font-weight:600; padding:10px 12px; border-bottom:1px solid {BORDER}; }}
.dt-bench td {{ padding:12px; border-bottom:1px solid #EEF2F4; vertical-align:middle; color:#1E293B; }}
.dt-bench td.ds {{ font-family:{MONO}; font-size:.78rem; color:{SLATE}; }}
.dt-bench td.num {{ font-family:{MONO}; white-space:nowrap; }}
.dt-bench td.v2 {{ color:{TEAL}; font-weight:600; }}
.dt-bench .pct {{ font-size:.78rem; opacity:.8; }}
.dt-bench tr.kpi td {{ background:#F0FAF8; border-top:2px solid {TEAL_LINE}; border-bottom:2px solid {TEAL_LINE}; }}
.dt-bench .bad {{ background:{RED_TINT}; color:{RED}; border:1px solid {RED_LINE}; border-radius:6px; padding:3px 7px; }}
.dt-bench .warn {{ background:{AMBER_TINT}; color:{AMBER}; border:1px solid {AMBER_LINE}; border-radius:6px;
      padding:3px 7px; }}
</style>
"""


def inject() -> None:
    st.html(CSS)


def esc(s) -> str:
    return html.escape(str(s))


def tag(text: str, ink: str, bg: str, line: str | None = None) -> str:
    return (f"<span class='dt-tag' style='color:{ink};background:{bg};border-color:{line or ink + '33'}'>"
            f"{esc(text)}</span>")


def sidebar_brand() -> str:
    return (f"<div style='display:flex;gap:11px;align-items:center;margin-bottom:12px'>"
            f"<div style='width:38px;height:38px;border-radius:10px;background:{TEAL};display:flex;"
            f"align-items:center;justify-content:center'>{PULSE_SVG}</div>"
            f"<div style='line-height:1.25'><div style='font-weight:700;font-size:1.08rem;color:{INK}'>Dr.Tracker</div>"
            f"<div style='color:{TEAL};font-size:.88rem'>CINV Symptom Monitor</div></div></div>"
            f"<div style='background:#fff;border:1px solid {BORDER};border-radius:10px;padding:8px 12px;"
            f"font-size:.8rem;color:#334155;display:flex;gap:8px;align-items:center'>"
            f"<span style='width:7px;height:7px;border-radius:50%;background:#F59E0B;flex:none'></span>"
            f"Synthetic Demo Data · Not Clinical Advice</div>")


def nav_badges(attention: int, pid: str) -> None:
    """Badge text for the sidebar nav links (drawn with CSS ::after on the keyed containers)."""
    over = f'content:"{attention}";' if attention else "display:none;"
    st.html(f"<style>.st-key-nav_overview a::after{{{over}}}"
            f".st-key-nav_deepdive a::after{{content:\"{esc(pid)}\";}}</style>")
