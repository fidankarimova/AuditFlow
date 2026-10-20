"""Negotiation engine (coordinator side), section 5.4 of the build plan.

1. BILNO computes the reference point (x*, d*) using ONLY each workspace's /evaluate (feasible + gain bucket).
2. Seller Agent opens; Buyer Agent evaluates privately and counters; at most 3 rounds each way. Agents run in
   their own workspaces; the coordinator only relays offers (rationales are untrusted text for the other side).
3. Close only when one side accepts the other's exact offer AND both workspaces still call it feasible AND its
   Nash product is within 10% of the BILNO optimum. Otherwise NO_FEASIBLE_AGREEMENT + the best alternative.
"""

from __future__ import annotations

import uuid

import httpx

from engine import bilno

MAX_ROUNDS = 3
NASH_TOLERANCE = 0.10


class BudgetExhausted(Exception):
    pass


def _evaluator(client: httpx.Client, negotiation_id: str, invoice: str, counter: dict, side: str):
    def evaluate(x: float, d: float) -> dict:
        r = client.post("/evaluate", json={"negotiation_id": negotiation_id, "invoice_number": invoice, "x": x, "d": d})
        if r.status_code == 429:
            raise BudgetExhausted(f"{side}: {r.json().get('detail')}")
        r.raise_for_status()
        counter[side] += 1
        return r.json()

    return evaluate


def negotiate(supplier: httpx.Client, buyer: httpx.Client, invoice: str, allow_partial: bool = True,
              negotiation_id: str | None = None) -> dict:
    negotiation_id = negotiation_id or f"neg_{uuid.uuid4().hex[:12]}"
    counter = {"supplier": 0, "buyer": 0}
    s_eval = _evaluator(supplier, negotiation_id, invoice, counter, "supplier")
    b_eval = _evaluator(buyer, negotiation_id, invoice, counter, "buyer")
    summary = supplier.get(f"/negotiation-summary/{invoice}").raise_for_status().json()
    transcript = {"negotiation_id": negotiation_id, "invoice_number": invoice, "rounds": [],
                  "evaluations": counter, "outcome": None, "close_check": None}

    try:
        ref = bilno.decide(summary, s_eval, b_eval, allow_partial)
    except BudgetExhausted as e:
        transcript.update(outcome=bilno.NO_FEASIBLE_AGREEMENT, reason=f"evaluation budget exhausted: {e}",
                          recommendation=summary.get("best_alternative"))
        return transcript
    transcript["bilno"] = ref.as_dict()
    transcript["bilno_trace"] = ref.trace  # which (x, d) points were asked and the yes/no answers only
    if ref.status != bilno.AGREEMENT:
        transcript.update(outcome=ref.status, recommendation=ref.recommendation)
        return transcript

    reference = {"x": ref.x, "d": ref.d}
    history, last, agreed = [], None, None
    for rnd in range(1, MAX_ROUNDS + 1):
        for side, client in (("seller", supplier), ("buyer", buyer)):
            r = client.post(f"/agents/{side}/turn", json={
                "negotiation_id": negotiation_id, "invoice_number": invoice, "round": rnd,
                "reference": reference, "last_offer": last, "history": history})
            r.raise_for_status()
            turn = r.json()
            transcript["rounds"].append(turn)
            history.append({k: turn[k] for k in ("side", "round", "action", "x", "d", "rationale")})
            if turn["action"] == "walk_away":
                break
            if turn["action"] == "accept":
                agreed = {"x": turn["x"], "d": turn["d"]}
                break
            last = {"x": turn["x"], "d": turn["d"]}
        if agreed or turn["action"] == "walk_away":
            break

    if agreed is None:
        transcript.update(outcome=bilno.NO_FEASIBLE_AGREEMENT, reason="agents did not agree within 3 rounds",
                          recommendation=summary.get("best_alternative"))
        return transcript

    try:
        s, b = s_eval(agreed["x"], agreed["d"]), b_eval(agreed["x"], agreed["d"])
    except BudgetExhausted as e:
        transcript.update(outcome=bilno.NO_FEASIBLE_AGREEMENT, reason=f"evaluation budget exhausted: {e}",
                          recommendation=summary.get("best_alternative"))
        return transcript
    nash = s["gain_bucket"] * bilno.GAIN_BUCKET * b["gain_bucket"] * bilno.GAIN_BUCKET
    ok = (s["feasible"] and b["feasible"] and s["gain_bucket"] > 0 and b["gain_bucket"] > 0
          and nash >= (1 - NASH_TOLERANCE) * ref.nash_product)
    transcript["close_check"] = {"supplier_feasible": s["feasible"], "buyer_feasible": b["feasible"],
                                 "nash_product": nash, "optimum_nash_product": ref.nash_product,
                                 "within_tolerance": ok}
    if not ok:
        transcript.update(outcome=bilno.NO_FEASIBLE_AGREEMENT, reason="agreed terms failed the BILNO close check",
                          recommendation=summary.get("best_alternative"))
        return transcript
    transcript.update(outcome=bilno.AGREEMENT, agreed=agreed, recommendation="early_payment")
    return transcript
