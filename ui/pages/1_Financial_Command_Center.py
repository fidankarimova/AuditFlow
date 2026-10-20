import pandas as pd
import streamlit as st

from ui.common import call, ok, role_key, setup, state_badge, tl

role = setup("Financial Command Center", "🏦")
who = role_key(role)

if who == "supplier":
    company = ok("supplier", "GET", "/company")
    if company is None:
        st.stop()
    if st.button("Re-run Risk and Strategy agents") or "risk" not in st.session_state:
        with st.spinner("Running Monte Carlo stress test (5,000 paths x 3 scenarios)..."):
            st.session_state["risk"] = ok("supplier", "POST", "/agents/risk/run")
            st.session_state["strategy"] = ok("supplier", "POST", "/agents/strategy/run")
    risk, strategy = st.session_state.get("risk"), st.session_state.get("strategy")
    obligations = ok("supplier", "GET", "/obligations") or []
    receivables = ok("supplier", "GET", "/receivables") or []

    if risk and risk["crisis"]:
        st.error(f"⚠️ **Cash crisis in {risk['days_to_crisis']} days.** {risk['message']}")
    elif risk:
        st.success(f"✅ {risk['message']}")
    if strategy:
        st.info(f"**Recommended action:** {strategy['message']}")

    c = st.columns(4)
    c[0].metric("Cash today", tl(company["cash"]))
    c[1].metric("Minimum reserve", tl(company["reserve"]))
    c[2].metric("Obligations (next 60 days)", tl(sum(o["amount"] for o in obligations)))
    c[3].metric("Open receivables", tl(sum(r["face"] for r in receivables if r["status"] == "open")))

    left, right = st.columns(2)
    with left:
        st.subheader("Obligations")
        st.dataframe(pd.DataFrame(obligations)[["date", "category", "amount"]], hide_index=True,
                     use_container_width=True)
    with right:
        st.subheader("Receivables")
        df = pd.DataFrame(receivables)[["number", "customer", "face", "due_date", "status"]]
        st.dataframe(df, hide_index=True, use_container_width=True)
    if strategy and strategy.get("alternatives"):
        st.subheader("Alternatives without a deal (Strategy Agent)")
        alt = pd.DataFrame([{
            "alternative": a["name"], "amount (TL)": round(a["amount"]), "cost (TL)": round(a["cost"]),
            "APR": f"{a['apr']:.1%}" if a["apr"] is not None else "-", "arrives (day)": a["arrival_day"],
            "P(breach) after": f"{a['p_breach_after']:.1%}", "feasible": "yes" if a["feasible"] else "no",
            "reason": a["reason"]} for a in strategy["alternatives"]])
        st.dataframe(alt, hide_index=True, use_container_width=True)
        st.caption("Early payment is priced by BILNO in the Negotiation Room against the best of these.")
    with st.expander("Agent tool calls"):
        st.json({"risk_agent": (risk or {}).get("tool_calls"), "strategy_agent": (strategy or {}).get("tool_calls")})

elif who == "buyer":
    st.caption("Beta Markets' own private view. These numbers never leave the buyer workspace.")
    company = ok("buyer", "GET", "/internal/company")
    forecast = ok("buyer", "GET", "/internal/forecast")
    if company and forecast:
        c = st.columns(4)
        c[0].metric("Cash today", tl(company["cash"]))
        c[1].metric("Minimum reserve (private)", tl(company["reserve"]))
        c[2].metric("Max early cash (private)", tl(company["max_early_cash"]))
        c[3].metric("Reserve breach probability", f"{forecast['p_breach']:.1%}")
        st.subheader("Payables")
        st.dataframe(pd.DataFrame(ok("buyer", "GET", "/internal/payables") or []), hide_index=True,
                     use_container_width=True)

else:
    st.caption("Auditor view: every deal the coordinator has handled.")
    code, deals = call("coordinator", "GET", "/deals")
    if deals:
        for d in deals:
            st.markdown(f"- `{d['deal_id']}` · invoice {d['invoice_number']} · {state_badge(d['state'])}")
    else:
        st.write("No deals yet.")
