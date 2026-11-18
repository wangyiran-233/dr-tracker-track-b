"""Dr.Tracker Track B prototype dashboard (synthetic data only).

Two clinical views (Overview, Patient Deep-Dive). The developer page "Parser & Eval
Inspector" (patient words vs parsed data, review queue, method, evaluation) appears
only when the sidebar toggle "Show Parser & Eval Inspector" is on (off by default).

Run:
    .venv\\Scripts\\python.exe -m streamlit run app.py
"""

import streamlit as st

from dashboard import style, views

st.set_page_config(page_title="Dr.Tracker · CINV Symptom Monitor (demo)", page_icon="🩺", layout="wide")
style.inject()

pages = {
    "overview": st.Page(views.page_overview, title="Overview", icon=":material/grid_view:",
                        url_path="overview", default=True),
    "deepdive": st.Page(views.page_deepdive, title="Patient Deep-Dive", icon=":material/person:",
                        url_path="patient"),
    "inspector": st.Page(views.page_inspector, title="Parser & Eval Inspector", icon=":material/code:",
                         url_path="inspector"),
}
views.PAGES = pages

# Sidebar, top to bottom: brand, clinical workflow nav, developer-mode card (pinned to the bottom by CSS).
sb = st.sidebar
sb.html(style.sidebar_brand())
sb.html("<div class='dt-label' style='margin:18px 0 2px 6px'>Clinical workflow</div>")
nav_box = sb.container(gap="small")
with sb.container(key="devbox"):
    st.html("<div class='dt-label' style='margin:0 0 6px 2px'>Demo / developer mode</div>")
    with st.container(key="devcard"):
        dev = st.toggle("Show Parser & Eval Inspector", value=False, key="dev_mode",
                        help="Audit view for evaluators: patient words vs parsed rows, review queue, "
                             "parser method and benchmark results. Not part of the clinical workflow.")
        st.caption("Audit LLM extraction, review flags & parser evaluation")

nav = [pages["overview"], pages["deepdive"]] + ([pages["inspector"]] if dev else [])
pg = st.navigation(nav, position="hidden")

with nav_box:
    for name in ("overview", "deepdive") + (("inspector",) if dev else ()):
        with st.container(key=f"nav_{name}"):
            st.page_link(pages[name], width="stretch")
style.nav_badges(views.attention_count(), views.current_pid())
pg.run()
