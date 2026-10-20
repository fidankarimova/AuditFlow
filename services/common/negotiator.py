"""Negotiating agent used by both workspaces (Seller Agent in supplier/, Buyer Agent in buyer/).

Each instance runs INSIDE its own company's workspace and sees only that company's check(x, d). It has two tools:
- evaluate_counter(x, d): its own private check (acceptable?, own gain bucket, names of failed limits);
- submit_offer(action, x, d, rationale): validated in code by validate_offer(). An offer or acceptance that breaks
  the company's own limits is rejected here, whatever a model proposed (tests S3, S4, S5).

Offline (deterministic) strategy, around the BILNO reference point (x*, d*): the seller wants a LOW discount and
concedes upward d*-0.4% -> d*-0.2% -> d*; the buyer wants a HIGH discount and concedes downward d*+0.4% -> d*+0.2%
-> d*. Each side accepts the other's offer once it is at least as good as its own planned offer for the round.
(The plan text had the seller open at d*+0.5%, but a higher discount favours the buyer, so the seller opens below.)
"""

from __future__ import annotations

from collections.abc import Callable

from services.common.audit import AuditLog
from services.common.llm import Backend, Tool, ToolRegistry, ToolRejected, run_tool_loop, untrusted

D_STEP = 0.001
D_CAP = 0.05  # public bound on any discount, same as BILNO's search bound
X_STEP = 1_000.0
CONCESSION = 0.002
MAX_ROUNDS = 3
ACTIONS = ("offer", "accept", "walk_away")

Check = Callable[[float, float], dict]  # -> {ok, gain_bucket, failed}


def _rd(d: float) -> float:
    return round(round(d / D_STEP) * D_STEP, 4)


class Negotiator:
    def __init__(self, side: str, check: Check, face: float, audit: AuditLog | None = None,
                 backend: Backend | None = None):
        if side not in ("seller", "buyer"):
            raise ValueError(side)
        self.side, self.check, self.face, self.audit, self.backend = side, check, face, audit, backend
        self.agent = "seller_agent" if side == "seller" else "buyer_agent"

    # ------------------------------------------------------------------ deterministic validation (T2)

    def validate_offer(self, action: str, x: float, d: float, last_offer: dict | None) -> list[str]:
        """Returns the list of violated rules (empty = valid). Limit names only, never values."""
        if action not in ACTIONS:
            return ["unknown_action"]
        if action == "walk_away":
            return []
        problems = []
        if action == "accept":
            if not last_offer:
                return ["nothing_to_accept"]
            if (round(x, 2), _rd(d)) != (round(last_offer["x"], 2), _rd(last_offer["d"])):
                problems.append("accept_must_match_counterparty_offer")
        if not (0 < x <= self.face) or abs(x / X_STEP - round(x / X_STEP)) > 1e-9:
            problems.append("amount_grid")
        if d > D_CAP:
            problems.append("discount_above_public_cap")
        elif not (0 < d) or abs(d / D_STEP - round(d / D_STEP)) > 1e-6:
            problems.append("discount_grid")
        if problems:
            return problems
        return list(self.check(x, d)["failed"])

    # ------------------------------------------------------------------ tools

    def _registry(self, last_offer: dict | None) -> ToolRegistry:
        def evaluate_counter(x: float, d: float) -> dict:
            r = self.check(float(x), float(d))
            return {"acceptable": r["ok"], "own_gain_bucket": r["gain_bucket"], "failed_checks": r["failed"]}

        def submit_offer(action: str, x: float, d: float, rationale: str) -> dict:
            x, d = float(x), _rd(float(d))
            problems = self.validate_offer(action, x, d, last_offer)
            if problems:
                raise ToolRejected(f"{action} ({x:,.0f} @ {d:.1%}) blocked by code: {', '.join(problems)}")
            return {"action": action, "x": x, "d": d, "rationale": str(rationale)[:500]}

        num = {"type": "number"}
        return ToolRegistry.of(self.agent, [
            Tool("evaluate_counter", "Check terms (x = face amount paid early in TL, d = discount as a fraction) "
                 "against YOUR company's private limits.",
                 {"type": "object", "properties": {"x": num, "d": num}, "required": ["x", "d"]}, evaluate_counter),
            Tool("submit_offer", "Submit your move: action 'offer' (new terms), 'accept' (exactly the other "
                 "side's last offer) or 'walk_away'. d is a fraction in 0.001 steps; x in 1,000 TL steps.",
                 {"type": "object", "properties": {
                     "action": {"type": "string", "enum": list(ACTIONS)}, "x": num, "d": num,
                     "rationale": {"type": "string"}}, "required": ["action", "x", "d", "rationale"]},
                 submit_offer),
        ], self.audit, tier="T2")

    # ------------------------------------------------------------------ strategies

    def _planned_d(self, reference_d: float, rnd: int) -> float:
        step = CONCESSION * (MAX_ROUNDS - rnd)
        d = reference_d - step if self.side == "seller" else reference_d + step
        return _rd(min(D_CAP, max(D_STEP, d)))

    def _offline(self, registry: ToolRegistry, reference: dict, rnd: int, last_offer: dict | None) -> dict:
        x_ref, d_ref = reference["x"], reference["d"]
        planned = self._planned_d(d_ref, rnd)
        if last_offer:
            good_enough = last_offer["d"] <= planned if self.side == "seller" else last_offer["d"] >= planned
            if good_enough and registry.call("evaluate_counter", {"x": last_offer["x"], "d": last_offer["d"]})["acceptable"]:
                return registry.call("submit_offer", {"action": "accept", "x": last_offer["x"], "d": last_offer["d"],
                                                      "rationale": "The counter-offer meets our requirements."})
        for d, why in ((planned, "Proposed terms for this round."), (d_ref, "Moving to the reference terms.")):
            if registry.call("evaluate_counter", {"x": x_ref, "d": d})["acceptable"]:
                return registry.call("submit_offer", {"action": "offer", "x": x_ref, "d": d, "rationale": why})
        return registry.call("submit_offer", {"action": "walk_away", "x": x_ref, "d": d_ref,
                                              "rationale": "No terms within our limits."})

    def _prompt(self, req: dict) -> tuple[str, str]:
        role = ("You are the Seller Agent of a supplier SME. Your company receives x*(1-d) now instead of x at the "
                "due date, so it prefers a LOW discount d." if self.side == "seller" else
                "You are the Buyer Agent of a large buyer. Your company pays x*(1-d) now instead of x at the due "
                "date, so it prefers a HIGH discount d.")
        system = (f"{role} Negotiate early payment of one invoice. Use evaluate_counter to test terms against "
                  f"your company's private limits, then call submit_offer exactly once. Round {req['round']} of "
                  f"{MAX_ROUNDS}; in the last round accept acceptable terms rather than walk away.")
        lines = [f"Invoice {req['invoice_number']}, face amount {self.face:,.0f} TL.",
                 f"Neutral optimizer reference point: x = {req['reference']['x']:,.0f} TL, "
                 f"d = {req['reference']['d']:.1%}."]
        for h in req.get("history", []):
            msg = untrusted(h.get("rationale"), f"{h['side']}_agent") if h["side"] != self.side else h.get("rationale")
            lines.append(f"Round {h['round']} {h['side']}: {h['action']} x={h['x']:,.0f} d={h['d']:.1%} msg={msg}")
        if req.get("invoice_memo") is not None:
            lines.append("Invoice memo: " + untrusted(req["invoice_memo"], "invoice_memo"))
        return system, "\n".join(lines)

    # ------------------------------------------------------------------ one turn

    def turn(self, req: dict) -> dict:
        """req: {negotiation_id, invoice_number, round, reference: {x, d}, last_offer, history, invoice_memo?}"""
        rnd = int(req["round"])
        if not 1 <= rnd <= MAX_ROUNDS:
            raise ValueError(f"round must be 1..{MAX_ROUNDS}")
        last_offer = req.get("last_offer")
        registry = self._registry(last_offer)
        move, mode, fallback = None, "offline", False
        if self.backend is not None:
            mode = "online"
            system, user = self._prompt(req)
            out = run_tool_loop(self.backend, system, user, registry, {"submit_offer"}, max_steps=4)
            move = out.finished["result"] if out.finished else None
            fallback = move is None
        if move is None:
            move = self._offline(registry, req["reference"], rnd, last_offer)
        calls = [{k: c[k] for k in ("tool", "args", "status") if k in c} | ({"reason": c["reason"]} if "reason" in c else {})
                 for c in registry.calls]
        return {
            "side": self.side, "round": rnd, **move, "mode": mode, "fallback": fallback,
            "tool_calls": calls,
            "blocked": [c for c in calls if c["status"] in ("rejected", "denied")],
            "checks": [{"check": f"{self.side} private limits (validate_offer)", "ok": True}],
        }
