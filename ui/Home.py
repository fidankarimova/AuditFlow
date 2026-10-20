"""AuditFlow AI demo UI (home page). Run: make ui (services must be running: make up)."""

import streamlit as st

from ui.common import call, setup

role = setup("AuditFlow AI")
st.markdown(
    "**Privacy-preserving multi-agent working capital optimization.** A supplier's AI agents detect a cash crisis, "
    "a Seller Agent and a Buyer Agent negotiate early payment of an invoice inside each company's private limits, "
    "and a deterministic optimizer (BILNO) checks that the deal is good for both sides, or refuses it. Both "
    "companies' authorized humans sign the exact terms. A simulated bank executes the payment, and every step "
    "is recorded on the ledger and in a hash-chained audit log."
)
st.info("*A good financial agent is not one that always closes a deal; it is one that refuses a bad one.*")

st.subheader("Services")
cols = st.columns(4)
for col, (name, path) in zip(cols, [("supplier", "/company"), ("buyer", "/docs"), ("coordinator", "/health"),
                                    ("bank", "/health")]):
    code, body = call(name, "GET", path, quiet=True)
    with col:
        if code == 200:
            st.success(f"{name}: up", icon="✅")
            if name == "coordinator":
                target = body.get("ledger_target")
                st.caption(f"Ledger: {target}" + (" (disabled, no on-chain records)" if target == "none" else ""))
        else:
            st.error(f"{name}: down", icon="⛔")
if any(call(n, "GET", p, quiet=True)[0] != 200 for n, p in [("coordinator", "/health"), ("bank", "/health")]):
    st.warning("Start the services first: `make up` (in another terminal).")

st.subheader("Demo path")
st.markdown("""
1. **Financial Command Center**: the Risk Agent's crisis alert and the Strategy Agent's recommendation.
2. **Liquidity Stress Lab**: Monte Carlo bands, `p_breach` per scenario, before vs after the action.
3. **Intelligent Receivables**: which invoices are eligible, and why the others are excluded.
4. **Live AI Negotiation Room**: verify the invoice, run the negotiation, see every offer and blocked attempt.
5. **Privacy & Compliance Verification**: what each side revealed, evaluation counters, compliance checks.
6. **Agreement & Payment Approval**: exact terms and `agreement_hash`; each company signs (EIP-712).
7. **Settlement & Audit Explorer**: bank payment, webhooks, ledger txs, audit chain, attack buttons.
""")
