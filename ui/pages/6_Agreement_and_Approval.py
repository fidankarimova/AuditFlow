import pandas as pd
import streamlit as st

from ui.common import current_deal, minor_tl, ok, role_key, setup, state_badge

role = setup("Agreement & Payment Approval", "✍️")
who = role_key(role)
deal = current_deal()
if deal is None or not deal.get("agreement"):
    st.info("No agreement yet. Run a negotiation first.")
    st.stop()

a = deal["agreement"]
t = a["agreement"]
st.markdown(f"Deal `{deal['deal_id']}` · {state_badge(deal['state'])} · agreement **v{a['version']}**")
st.code(a["agreement_hash"], language=None)
st.caption("agreement_hash = EIP-712 digest of the exact terms below (domain: AuditFlow AgreementLedger). Each "
           "company's authorized human signs this hash. Any change gives a new version and a new hash.")

c = st.columns(4)
c[0].metric("Face amount paid early", minor_tl(t["face_amount_x"]))
c[1].metric("Discount", f"{t['discount_bps'] / 100:.2f}%")
c[2].metric("Buyer pays now", minor_tl(t["pay_amount"]))
c[3].metric("Currency", t["currency"])
st.caption(f"Payee account: `{t['payee_account']}` (the seller's registered account) · expires at "
           f"{pd.to_datetime(t['expires_at'], unit='s', utc=True):%Y-%m-%d %H:%M} UTC")
with st.expander("Canonical agreement JSON"):
    st.json(t)

st.subheader("Approvals (T3)")
cols = st.columns(2)
for col, side in zip(cols, ("supplier", "buyer")):
    with col:
        signed = a[f"{side}_signed"]
        st.markdown(f"**{'Alpha Foods (seller)' if side == 'supplier' else 'Beta Markets (buyer)'}**: "
                    f"{'✅ signed' if signed else '⏳ not signed'}")
        st.caption(f"Approver address {t['seller' if side == 'supplier' else 'buyer']}")
        can_sign = who == side and not signed and deal["state"] == "NEGOTIATED"
        if st.button(f"Sign as {side}", key=f"sign-{side}", disabled=not can_sign, type="primary",
                     help="Switch the role in the sidebar to sign for this company. Demo: the key is held by "
                          "the server (documented known risk)."):
            if ok("coordinator", "POST", f"/deals/{deal['deal_id']}/sign", json={"role": side}):
                st.rerun()

st.subheader("Execution (T4)")
if deal["state"] == "APPROVED":
    if st.button("Execute bank payment", type="primary"):
        with st.spinner("Initiating bank payment with the agreement's idempotency key..."):
            if ok("coordinator", "POST", f"/deals/{deal['deal_id']}/submit"):
                st.rerun()
else:
    st.caption(f"The coordinator only executes in state APPROVED (both signatures over this exact version). "
               f"Current state: {deal['state']}.")

if deal["state"] in ("NEGOTIATED", "APPROVED"):
    with st.expander("Change the terms (creates a new version; existing signatures stop applying)"):
        new_x = st.number_input("New face amount paid early (TL)", 1_000.0, float(deal["invoice"]["face"]),
                                t["face_amount_x"] / 100 - 2_000, step=1_000.0)
        if st.button("Amend agreement"):
            if ok("coordinator", "POST", f"/deals/{deal['deal_id']}/amend", json={"face_amount_x": new_x}):
                st.rerun()

st.subheader("Versions")
st.dataframe(pd.DataFrame([{"version": v["version"], "hash": v["agreement_hash"][:18] + "...",
                            "x (TL)": v["agreement"]["face_amount_x"] / 100,
                            "discount": f"{v['agreement']['discount_bps'] / 100:.2f}%",
                            "supplier signed": bool(v["supplier_signed"]), "buyer signed": bool(v["buyer_signed"]),
                            "status": "active" if v["active"] else "superseded"} for v in deal["versions"]]),
             hide_index=True, use_container_width=True)
