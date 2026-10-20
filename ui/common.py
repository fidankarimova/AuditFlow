"""Shared helpers for the Streamlit UI: service clients, role switcher, deal selector, charts.

The UI is a thin client over the services' HTTP APIs (start them with `make up`). It holds no business logic:
every check, limit and state change happens in the services. The role switcher only changes what this
demo UI shows; it is not authentication.
"""

from __future__ import annotations

import altair as alt
import httpx
import pandas as pd
import streamlit as st

from services.common.config import get_settings

ROLES = ("Supplier (Alpha Foods)", "Buyer (Beta Markets)", "Auditor")
SERIES_1, SERIES_2 = "#2a78d6", "#eb6834"  # categorical slots 1-2 (blue, orange)
CRITICAL, GOOD = "#d03b3b", "#0ca30c"  # status colors, always paired with a label
TEXT_2, GRID = "#52514e", "#e1e0d9"


@st.cache_resource
def _clients() -> dict[str, httpx.Client]:
    s = get_settings()
    urls = {"supplier": s.supplier_url, "buyer": s.buyer_url, "coordinator": s.coordinator_url, "bank": s.bank_url}
    return {name: httpx.Client(base_url=u, timeout=300) for name, u in urls.items()}


def call(service: str, method: str, path: str, quiet: bool = False, **kw):
    """Returns (status_code, json) or (None, None) if the service is down (with an error box)."""
    client = _clients()[service]
    try:
        r = client.request(method, path, **kw)
    except httpx.HTTPError:
        if not quiet:
            st.error(f"The {service} service is not reachable at {client.base_url}. Start the services with "
                     "`make up`.")
        return None, None
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return r.status_code, body


def ok(service: str, method: str, path: str, **kw):
    """Calls a service and shows the error detail if it fails. Returns the JSON body or None."""
    code, body = call(service, method, path, **kw)
    if code is None:
        return None
    if code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        st.error(f"{method} {path} -> {code}: {detail}")
        if isinstance(body, dict) and body.get("details"):
            st.json(body["details"], expanded=False)
        return None
    return body


def setup(title: str, icon: str = "") -> str:
    st.set_page_config(page_title=f"AuditFlow · {title}", page_icon=icon or None, layout="wide")
    with st.sidebar:
        st.markdown("### AuditFlow AI")
        role = st.radio("View as", ROLES, key="role",
                        help="Demo role switcher (not authentication). It changes what this UI shows and which "
                             "signature button is enabled.")
        st.divider()
        deal_selector()
        st.caption("Simulated bank · regulation-aware architecture · no production banking access")
    st.title(title)
    return role


def role_key(role: str) -> str:
    return "supplier" if role.startswith("Supplier") else "buyer" if role.startswith("Buyer") else "auditor"


def deal_selector() -> str | None:
    code, deals = call("coordinator", "GET", "/deals", quiet=True)
    if code != 200 or not deals:
        st.caption("No deal yet. Start one in the Negotiation Room.")
        st.session_state["deal_id"] = None
        return None
    ids = [d["deal_id"] for d in deals]
    labels = {d["deal_id"]: f"{d['deal_id']} · {d['invoice_number']} · {d['state']}" for d in deals}
    current = st.session_state.get("deal_id")
    index = ids.index(current) if current in ids else len(ids) - 1
    chosen = st.selectbox("Deal", ids, index=index, format_func=labels.get)
    st.session_state["deal_id"] = chosen
    return chosen


def current_deal() -> dict | None:
    deal_id = st.session_state.get("deal_id")
    if not deal_id:
        return None
    code, deal = call("coordinator", "GET", f"/deals/{deal_id}", quiet=True)
    return deal if code == 200 else None


def tl(amount: float | None) -> str:
    return "-" if amount is None else f"{amount:,.0f} TL"


def minor_tl(amount_minor: int | None) -> str:
    return "-" if amount_minor is None else f"{amount_minor / 100:,.2f} TL"


STATE_BADGE = {"SETTLED": "🟢", "FAILED": "🔴", "REJECTED": "⚪", "EXPIRED": "⚪", "CANCELLED": "⚪"}


def state_badge(state: str) -> str:
    return f"{STATE_BADGE.get(state, '🔵')} **{state}**"


# ----------------------------------------------------------------------------- charts


def _axis_x():
    return alt.X("day:Q", title="Days from today", axis=alt.Axis(grid=False, tickMinStep=5))


def _axis_y(title="Cash (TL)"):
    return alt.Y("value:Q", title=title, axis=alt.Axis(format=",.0f", gridColor=GRID))


def _reserve_rule(reserve: float):
    base = alt.Chart(pd.DataFrame({"reserve": [reserve], "label": [f"Reserve {reserve:,.0f} TL"]}))
    rule = base.mark_rule(color=CRITICAL, strokeWidth=1.5).encode(y="reserve:Q")
    text = base.mark_text(align="left", dx=4, dy=-6, color=TEXT_2, fontSize=11).encode(
        y="reserve:Q", x=alt.value(0), text="label:N")
    return rule + text


def fan_chart(bands: dict, reserve: float, height: int = 320) -> alt.Chart:
    """p5-p95 band (wash) + median line + reserve threshold, with a crosshair tooltip."""
    df = pd.DataFrame({"day": range(len(bands["p50"])), "p5": bands["p5"], "p50": bands["p50"],
                       "p95": bands["p95"]})
    band = alt.Chart(df).mark_area(color=SERIES_1, opacity=0.12).encode(
        x=_axis_x(), y=alt.Y("p5:Q", title="Cash (TL)", axis=alt.Axis(format=",.0f", gridColor=GRID)), y2="p95:Q")
    median = alt.Chart(df).mark_line(color=SERIES_1, strokeWidth=2).encode(x="day:Q", y="p50:Q")
    hover = alt.selection_point(fields=["day"], nearest=True, on="pointermove", empty=False)
    rule = alt.Chart(df).mark_rule(color=TEXT_2, strokeWidth=1).encode(
        x="day:Q", opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
        tooltip=[alt.Tooltip("day:Q", title="Day"), alt.Tooltip("p50:Q", title="Median", format=",.0f"),
                 alt.Tooltip("p5:Q", title="p5 (bad case)", format=",.0f"),
                 alt.Tooltip("p95:Q", title="p95 (good case)", format=",.0f")]).add_params(hover)
    return (band + median + _reserve_rule(reserve) + rule).properties(height=height)


def before_after_chart(before: dict, after: dict, reserve: float, height: int = 340) -> alt.Chart:
    rows = []
    for label, b in (("Before action", before), ("After action", after)):
        for day, (lo, mid, hi) in enumerate(zip(b["p5"], b["p50"], b["p95"])):
            rows.append({"day": day, "scenario": label, "p5": lo, "p50": mid, "p95": hi})
    df = pd.DataFrame(rows)
    color = alt.Color("scenario:N", scale=alt.Scale(domain=["Before action", "After action"],
                                                    range=[SERIES_1, SERIES_2]),
                      legend=alt.Legend(title=None, orient="top"))
    band = alt.Chart(df).mark_area(opacity=0.10).encode(
        x=_axis_x(), y=alt.Y("p5:Q", title="Cash (TL)", axis=alt.Axis(format=",.0f", gridColor=GRID)),
        y2="p95:Q", color=alt.Color("scenario:N", scale=color.scale, legend=None))
    lines = alt.Chart(df).mark_line(strokeWidth=2).encode(x="day:Q", y="p50:Q", color=color)
    wide = df.pivot(index="day", columns="scenario", values="p50").reset_index()
    hover = alt.selection_point(fields=["day"], nearest=True, on="pointermove", empty=False)
    rule = alt.Chart(wide).mark_rule(color=TEXT_2).encode(
        x="day:Q", opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
        tooltip=[alt.Tooltip("day:Q", title="Day"),
                 alt.Tooltip("Before action:Q", title="Median before", format=",.0f"),
                 alt.Tooltip("After action:Q", title="Median after", format=",.0f")]).add_params(hover)
    return (band + lines + _reserve_rule(reserve) + rule).properties(height=height)
