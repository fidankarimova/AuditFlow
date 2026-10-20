import json
import time

import pandas as pd
import streamlit as st

from ui.common import call, current_deal, ok, setup, state_badge

setup("Settlement & Audit Explorer", "🔎")
deal = current_deal()

if deal:
    st.markdown(f"Deal `{deal['deal_id']}` · {state_badge(deal['state'])} · ledger: `{deal['ledger_target']}`"
                + (" (disabled: nothing is on-chain)" if deal["ledger_target"] == "none" else ""))
    if deal["state"] == "SUBMITTED":
        st.info("Waiting for the bank's signed webhook...")
        if st.button("Refresh"):
            st.rerun()

    st.subheader("State timeline")
    st.dataframe(pd.DataFrame(deal["history"]), hide_index=True, use_container_width=True)

    st.subheader("Ledger transactions")
    txs = pd.DataFrame(deal["ledger_txs"])
    if not txs.empty:
        st.dataframe(txs, hide_index=True, use_container_width=True,
                     column_config={"explorer_url": st.column_config.LinkColumn("explorer")})

    if deal.get("payment_id"):
        st.subheader("Bank payment")
        code, p = call("bank", "GET", f"/payments/{deal['payment_id']}")
        if code == 200:
            c = st.columns(3)
            c[0].metric("Payment id", p["payment_id"])
            c[1].metric("Bank status", p["status"])
            c[2].metric("Amount", f"{p['amount_minor'] / 100:,.2f} {p['currency']}")
            st.dataframe(pd.DataFrame(p["history"]), hide_index=True, use_container_width=True)
        if st.button("Poll bank (does not settle; only a signed webhook can)"):
            ok("coordinator", "POST", f"/deals/{deal['deal_id']}/refresh-payment")
            st.rerun()

st.subheader("Bank webhooks received by the coordinator")
events = ok("coordinator", "GET", "/bank/webhook-events") or []
if events:
    df = pd.DataFrame(events)[["seq", "received_at", "payment_id", "status", "verdict", "reason"]]
    st.dataframe(df, hide_index=True, use_container_width=True)
else:
    st.caption("None yet.")

st.subheader("Attack simulations")
a1, a2 = st.columns(2)
with a1:
    st.markdown("**Replay the bank payment request** (same Idempotency-Key, S7)")
    agreement = (deal or {}).get("agreement")
    if deal and deal.get("payment_id") and agreement:
        t = agreement["agreement"]
        if st.button("Replay bank request x3"):
            body = {"debtor_account": "SBX-TR-BETA-0001", "creditor_account": t["payee_account"],
                    "amount_minor": t["pay_amount"], "currency": t["currency"], "reference": agreement["agreement_hash"]}
            results = [call("bank", "POST", "/payments", json=body,
                            headers={"Idempotency-Key": t["idempotency_key"]}) for _ in range(3)]
            ids = {r[1].get("payment_id") for r in results if r[0] is not None}
            all_payments = ok("bank", "GET", "/payments") or []
            same_ref = [x for x in all_payments if x["reference"] == agreement["agreement_hash"]]
            st.session_state["replay_result"] = (
                f"HTTP {[r[0] for r in results]} · payment ids returned: {sorted(ids)} · payments at the bank for "
                f"this agreement: **{len(same_ref)}**. No second debit.")
        if st.session_state.get("replay_result"):
            st.success(st.session_state["replay_result"])
    else:
        st.caption("Available after the payment is submitted.")
with a2:
    st.markdown("**Send a forged 'SETTLED' webhook** (no valid HMAC, S8)")
    if st.button("Send forged webhook"):
        fake = json.dumps({"event_id": "evt_forged", "payment_id": (deal or {}).get("payment_id") or "pay_x",
                           "status": "SETTLED", "amount_minor": 1, "currency": "TRY"}).encode()
        code, body = call("coordinator", "POST", "/webhooks/bank", content=fake,
                          headers={"X-Bank-Signature": f"t={int(time.time())},v1={'0' * 64}",
                                   "Content-Type": "application/json"})
        st.session_state["forged_result"] = (bool(code and code >= 400), f"Coordinator answered HTTP {code}: {body}")
    if st.session_state.get("forged_result"):
        rejected, msg = st.session_state["forged_result"]
        (st.success if rejected else st.error)(("Rejected. " if rejected else "ACCEPTED (unexpected). ") + msg)

st.subheader("Audit log")
if st.button("Verify audit chain", type="primary"):
    v = ok("coordinator", "GET", "/audit/verify")
    if v:
        (st.success if v["ok"] else st.error)(f"{'Chain intact' if v['ok'] else 'TAMPERED'}: {v['message']}")
entries = ok("coordinator", "GET", "/audit/entries", params={"limit": 300}) or []
if entries:
    df = pd.DataFrame(entries)[["seq", "ts", "actor", "action", "tier", "entry_hash"]]
    tier = st.multiselect("Tier", ["T1", "T2", "T3", "T4", "T5"], default=["T1", "T2", "T3", "T4", "T5"])
    flagged = st.checkbox("Only rejected / denied / blocked")
    df = df[df["tier"].isin(tier)]
    if flagged:
        df = df[df["action"].str.contains("rejected|denied|reverted|blocked")]
    st.dataframe(df.iloc[::-1], hide_index=True, use_container_width=True)
