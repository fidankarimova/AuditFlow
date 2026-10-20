import pandas as pd
import streamlit as st

from ui.common import before_after_chart, current_deal, fan_chart, ok, role_key, setup, tl

SCENARIOS = {"normal": "Normal", "late_payments": "Late payments (delays x1.5)",
             "stress": "Stress (delays x2, expenses +20%)"}

role = setup("Liquidity Stress Lab", "📉")
who = role_key(role)
st.caption("Monte Carlo: 5,000 paths, 60 days, fixed seed. Customer payment delays are bootstrapped from history.")

if who == "buyer":
    results = {s: ok("buyer", "GET", "/internal/forecast", params={"scenario": s, "bands": True}) for s in SCENARIOS}
    reserve = (ok("buyer", "GET", "/internal/company") or {}).get("reserve", 0)
else:
    results = {s: ok("supplier", "GET", "/forecast", params={"scenario": s, "bands": True}) for s in SCENARIOS}
    reserve = (ok("supplier", "GET", "/company") or {}).get("reserve", 0)
if any(r is None for r in results.values()):
    st.stop()

cols = st.columns(3)
for col, (s, label) in zip(cols, SCENARIOS.items()):
    r = results[s]
    col.metric(f"P(cash < reserve), {label}", f"{r['p_breach']:.1%}",
               help=f"Expected max shortfall {tl(r['expected_max_shortfall'])}; first critical date "
                    f"{r['first_critical_date'] or 'none'}")

tabs = st.tabs(list(SCENARIOS.values()))
for tab, s in zip(tabs, SCENARIOS):
    with tab:
        r = results[s]
        st.altair_chart(fan_chart(r, reserve), use_container_width=True)
        st.caption("Blue line: median path. Shaded band: 5th to 95th percentile. Red line: minimum reserve.")
        with st.expander("Table view"):
            st.dataframe(pd.DataFrame({"day": range(len(r["p50"])), "p5": r["p5"], "p50": r["p50"],
                                       "p95": r["p95"]}).round(0), hide_index=True, use_container_width=True)

if who == "supplier":
    st.subheader("Before vs after action")
    deal = current_deal()
    terms = (deal or {}).get("agreement", {}) or {}
    terms = terms.get("agreement") if terms else None
    default_inflow = terms["pay_amount"] / 100 if terms else 58_800.0
    default_face = terms["face_amount_x"] / 100 if terms else 60_000.0
    c1, c2, c3 = st.columns(3)
    inflow = c1.number_input("Cash received today (TL)", 0.0, 200_000.0, default_inflow, step=1_000.0)
    face = c2.number_input("Invoice face settled early (TL)", 0.0, 100_000.0, default_face, step=1_000.0,
                           help="This part of the invoice no longer arrives at its due date.")
    scenario = c3.selectbox("Scenario", list(SCENARIOS), format_func=SCENARIOS.get)
    if terms:
        st.caption(f"Defaults come from the current agreement ({deal['deal_id']}).")
    w = ok("supplier", "GET", "/forecast/what-if", params={
        "inflow_today": inflow, "scenario": scenario, "invoice_number": "ALF-2026-0142",
        "face_paid_early": face, "bands": True})
    if w:
        m = st.columns(3)
        m[0].metric("P(cash < reserve)", f"{w['p_breach_after']:.1%}", f"{w['p_breach_change']:+.1%}",
                    delta_color="inverse")
        m[1].metric("Expected max shortfall", tl(w["expected_max_shortfall_after"]),
                    f"{w['expected_max_shortfall_after'] - w['expected_max_shortfall_before']:+,.0f} TL",
                    delta_color="inverse")
        m[2].metric("First critical day", w["first_critical_day_after"] or "none",
                    f"was {w['first_critical_day_before'] or 'none'}", delta_color="off")
        st.altair_chart(before_after_chart(w["before"], w["after"], w["reserve"]), use_container_width=True)
