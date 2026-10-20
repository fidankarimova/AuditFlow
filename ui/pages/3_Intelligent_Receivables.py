import pandas as pd
import streamlit as st

from ui.common import ok, role_key, setup

role = setup("Intelligent Receivables", "🧾")
if role_key(role) == "buyer":
    st.info("This screen shows the supplier's receivables. Switch to the Supplier or Auditor view.")
    st.stop()

rows = ok("supplier", "GET", "/receivables")
if rows is None:
    st.stop()
st.caption("Only buyer-confirmed, undisputed, unsettled invoices can be paid early. At intake the coordinator "
           "also re-verifies the buyer's confirmation with the buyer's own EIP-712 signature; it does not trust "
           "this flag.")
eligible = [r for r in rows if r["eligible"]]
excluded = [r for r in rows if not r["eligible"]]

st.subheader(f"✅ Eligible ({len(eligible)})")
st.dataframe(pd.DataFrame([{"invoice": r["number"], "customer": r["customer"], "face (TL)": r["face"],
                            "due": r["due_date"], "why": "; ".join(r["reasons"])} for r in eligible]),
             hide_index=True, use_container_width=True)

st.subheader(f"⛔ Excluded ({len(excluded)})")
st.dataframe(pd.DataFrame([{"invoice": r["number"], "customer": r["customer"], "face (TL)": r["face"],
                            "due": r["due_date"], "reason": "; ".join(r["reasons"])} for r in excluded]),
             hide_index=True, use_container_width=True)

with st.expander("Invoice memos (untrusted text)"):
    st.caption("Memos are shown as data. Agents only ever see them inside <untrusted> tags.")
    for r in rows:
        st.text(f"{r['number']}: {r['memo']}")
