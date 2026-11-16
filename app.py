"""Dr.Tracker Track B prototype dashboard (synthetic data only).

Run:
    .venv\\Scripts\\python.exe -m streamlit run app.py
"""

import streamlit as st

from dashboard import views

st.set_page_config(page_title="CINV patient-reported data (demo)", page_icon="🧪", layout="wide")

pages = {
    "overview": st.Page(views.page_overview, title="Overview", icon="👥", url_path="overview", default=True),
    "trend": st.Page(views.page_trend, title="Patient trend", icon="📈", url_path="trend"),
    "cycles": st.Page(views.page_cycles, title="Cycle comparison", icon="🔁", url_path="cycles"),
    "report": st.Page(views.page_report, title="Words vs parsed data", icon="🔎", url_path="report"),
    "method": st.Page(views.page_method, title="Method & evaluation", icon="🧪", url_path="method"),
}
views.PAGES = pages

st.navigation({"For the provider": [pages["overview"], pages["trend"], pages["cycles"]],
               "Under the hood": [pages["report"], pages["method"]]}).run()
