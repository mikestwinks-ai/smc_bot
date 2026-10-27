"""Local Streamlit dashboard. It reads only bot snapshots and read-only SQLite data."""

from typing import Callable

import streamlit as st

from smc.dashboard.pages import (
    REFRESH_INTERVALS_SECONDS,
    _with_refresh,
    render_execution,
    render_journal,
    render_market,
    render_model_analysis,
    render_news,
    render_overview,
    render_performance,
    render_risk,
    render_system_health,
)

def _page(render: Callable) -> Callable[[], None]:
    def run() -> None:
        _with_refresh(render)
    return run


st.set_page_config(page_title="SMC Trading Monitor", page_icon=":material/monitoring:", layout="wide")

with st.sidebar:
    st.subheader("SMC bot")
    refresh_seconds = st.select_slider(
        "Refresh interval",
        options=REFRESH_INTERVALS_SECONDS,
        value=5,
        format_func=lambda value: f"{value}s",
    )
    st.session_state["dashboard_refresh_seconds"] = refresh_seconds
    st.caption("Local read-only monitor · no order or position controls")

pages = [
    st.Page(_page(render_overview), title="Overview", url_path="overview", icon=":material/dashboard:"),
    st.Page(_page(render_market), title="Market", url_path="market", icon=":material/candlestick_chart:"),
    st.Page(_page(render_performance), title="Performance", url_path="performance", icon=":material/insights:"),
    st.Page(_page(render_model_analysis), title="Model analysis", url_path="model-analysis", icon=":material/analytics:"),
    st.Page(_page(render_execution), title="Execution", url_path="execution", icon=":material/receipt_long:"),
    st.Page(_page(render_risk), title="Risk centre", url_path="risk", icon=":material/shield:"),
    st.Page(_page(render_news), title="News centre", url_path="news", icon=":material/event:"),
    st.Page(_page(render_journal), title="Journal", url_path="journal", icon=":material/menu_book:"),
    st.Page(_page(render_system_health), title="System health", url_path="system-health", icon=":material/monitor_heart:"),
]
current_page = st.navigation(pages, position="sidebar")
st.title(current_page.title, icon=current_page.icon)
current_page.run()
