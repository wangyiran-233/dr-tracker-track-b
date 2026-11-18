"""Dr.Tracker Track B prototype dashboard (synthetic data only).

Two clinical views (Overview, Patient Deep-Dive). The developer pages (Words vs
parsed data, Method & evaluation) appear only when the sidebar toggle
"Show Parser & Eval Inspector" is on (off by default).

Run:
    .venv\\Scripts\\python.exe -m streamlit run app.py
"""

import streamlit as st

from dashboard import views

st.set_page_config(page_title="CINV patient-reported data (demo)", page_icon="🧪", layout="wide")

pages = {
    "overview": st.Page(views.page_overview, title="Overview", icon="👥", url_path="overview", default=True),
    "deepdive": st.Page(views.page_deepdive, title="Patient Deep-Dive", icon="🧑‍⚕️", url_path="patient"),
    "report": st.Page(views.page_report, title="Words vs parsed data", icon="🔎", url_path="report"),
    "method": st.Page(views.page_method, title="Method & evaluation", icon="🧪", url_path="method"),
}
views.PAGES = pages

# Sidebar: navigation is drawn at the top; these widgets follow it, at the bottom.
st.sidebar.caption("🧪 Synthetic data — not for clinical use.")
st.sidebar.divider()
st.sidebar.caption("DEMO / DEVELOPER MODE")
dev = st.sidebar.toggle("Show Parser & Eval Inspector", value=False, key="dev_mode",
                        help="Audit view for evaluators: patient words vs parsed rows, review queue, "
                             "parser method and benchmark results. Not part of the clinical workflow.")

nav = {"Clinical workflow": [pages["overview"], pages["deepdive"]]}
if dev:
    nav["Parser & Eval Inspector"] = [pages["report"], pages["method"]]
st.navigation(nav).run()
