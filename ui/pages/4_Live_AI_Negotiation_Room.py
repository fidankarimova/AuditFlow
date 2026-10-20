import pandas as pd
import streamlit as st

from ui.common import current_deal, ok, setup, state_badge

setup("Live AI Negotiation Room", "🤝")
deal = current_deal()

with st.expander("Start a new deal: verify an invoice", expanded=deal is None):
    receivables = ok("supplier", "GET", "/receivables") or []
    numbers = [r["number"] for r in receivables]
    labels = {r["number"]: f"{r['number']} · {r['customer']} · {r['face']:,.0f} TL"
                           f"{'' if r['eligible'] else ' (excluded: ' + '; '.join(r['reasons']) + ')'}"
              for r in receivables}
    if numbers:
        chosen = st.selectbox("Invoice", numbers, format_func=labels.get)
        st.caption("The coordinator runs compliance checks and asks the buyer workspace whether it owes this "
                   "invoice. The buyer's authorized human then confirms it with an EIP-712 signature (demo key). "
                   "Forged, disputed or duplicate invoices are refused here.")
        if st.button("Verify invoice", type="primary"):
            res = ok("coordinator", "POST", "/invoices/intake", json={"invoice_number": chosen})
            if res:
                st.session_state["deal_id"] = res["deal_id"]
                st.rerun()

if deal is None:
    st.stop()

st.markdown(f"Deal `{deal['deal_id']}` · invoice {deal['invoice']['number']} · {state_badge(deal['state'])}")
if deal["state"] == "VERIFIED":
    if st.button("Run negotiation", type="primary"):
        with st.spinner("BILNO search + agent rounds..."):
            if ok("coordinator", "POST", f"/deals/{deal['deal_id']}/negotiate"):
                st.rerun()
    st.stop()

neg = deal.get("negotiation")
if not neg:
    st.stop()
b = neg.get("bilno") or {}
c = st.columns(4)
if b.get("status") == "AGREEMENT":
    c[0].metric("BILNO optimum x*", f"{b['x']:,.0f} TL")
    c[1].metric("BILNO optimum d*", f"{b['d']:.1%}", help=f"Discount APR {b['apr']:.2%}")
    c[2].metric("Nash product (optimum)", f"{b['nash_product']:,.0f}")
    c[3].metric("Evaluations used", f"S {neg['evaluations']['supplier']} · B {neg['evaluations']['buyer']}",
                help="Limit: 200 per side per negotiation")

if neg["outcome"] == "AGREEMENT":
    st.success(f"✅ Agreement: x = {neg['agreed']['x']:,.0f} TL at d = {neg['agreed']['d']:.1%}. The BILNO close "
               f"check passed (Nash product within 10% of the optimum).")
else:
    st.warning(f"**{neg['outcome']}**: {neg.get('reason', '')}  \nRecommended instead: "
               f"**{neg.get('recommendation')}**")
    if neg["outcome"] == "NO_FEASIBLE_AGREEMENT":
        st.caption("No deal was made because no terms satisfy both sides' private limits and risk limits. "
                   "Refusing a bad deal is correct behaviour.")

st.subheader("Rounds")
for t in neg["rounds"]:
    icon = "🟦" if t["side"] == "seller" else "🟧"
    action = {"offer": "offers", "accept": "accepts", "walk_away": "walks away from"}[t["action"]]
    with st.chat_message("user" if t["side"] == "seller" else "assistant", avatar=icon):
        st.markdown(f"**Round {t['round']} · {t['side'].capitalize()} Agent {action}** "
                    f"x = {t['x']:,.0f} TL, d = {t['d']:.1%}  ·  mode: `{t['mode']}`"
                    f"{' · fallback to deterministic strategy' if t['fallback'] else ''}")
        st.caption(f"Rationale (untrusted for the other side): {t['rationale']}")
        for blk in t["blocked"]:
            st.error(f"Blocked by code: `{blk['tool']}` {blk.get('reason', '')}")
        for chk in t["checks"]:
            st.caption(f"{'✅' if chk['ok'] else '❌'} {chk['check']}")
        with st.expander("Tool calls"):
            st.dataframe(pd.DataFrame(t["tool_calls"]), hide_index=True, use_container_width=True)
if neg.get("close_check"):
    st.subheader("Close check")
    st.json(neg["close_check"])
