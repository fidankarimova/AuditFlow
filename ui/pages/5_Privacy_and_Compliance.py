import pandas as pd
import streamlit as st

from ui.common import current_deal, setup

setup("Privacy & Compliance Verification", "🔒")
st.warning("**Confidential policy verification (server-side). Not a zero-knowledge proof.** Each company's "
           "private limits are checked by code inside that company's own workspace; only the result is shared.",
           icon="🔒")
deal = current_deal()
if deal is None:
    st.info("No deal selected.")
    st.stop()

neg = deal.get("negotiation") or {}
st.subheader("What each side revealed")
st.markdown("""
| | Supplier (Alpha Foods) | Buyer (Beta Markets) |
|---|---|---|
| Kept private | cash, reserve, max discount, min net amount, bank loan terms, cost of best alternative | cash, reserve, opportunity cost, max early cash, risk limit |
| Revealed per `evaluate(x, d)` | `feasible` yes/no + gain rounded to 100 TL | `feasible` yes/no + gain rounded to 100 TL |
| Revealed to start | invoice number, face, days to due, whether action is needed, name of best alternative | nothing |
""")
st.caption("Known trade-off: the 100 TL gain bucket leaks coarse information. Limits: 200 evaluations per "
           "negotiation and 3 negotiations per invoice.")

if neg:
    ev = neg.get("evaluations", {})
    c = st.columns(3)
    c[0].metric("Supplier evaluations", f"{ev.get('supplier', 0)} / 200")
    c[1].metric("Buyer evaluations", f"{ev.get('buyer', 0)} / 200")
    c[2].metric("Points both sides accepted", (neg.get("bilno") or {}).get("feasible_points", "-"))
    trace = neg.get("bilno_trace") or []
    if trace:
        with st.expander(f"Every question the optimizer asked ({len(trace)}) and the yes/no answers"):
            df = pd.DataFrame(trace)
            df["d"] = (df["d"] * 100).round(1).astype(str) + "%"
            st.dataframe(df.rename(columns={"supplier_ok": "supplier feasible", "both_ok": "both feasible"}),
                         hide_index=True, use_container_width=True)

st.subheader("Compliance Agent checks")
st.caption("Deterministic checks; a failed blocking check stops the flow. The agent can flag but cannot change "
           "any policy or limit.")
for block in deal.get("compliance", []):
    st.markdown(f"**{block['subject'].capitalize()}**: {'✅ passed' if block['ok'] else '❌ failed'}")
    st.dataframe(pd.DataFrame([{"check": f["check"], "result": "pass" if f["ok"] else
                                ("FLAG" if not f["blocking"] else "FAIL"), "blocking": f["blocking"],
                                "detail": f["detail"]} for f in block["findings"]]),
                 hide_index=True, use_container_width=True)
