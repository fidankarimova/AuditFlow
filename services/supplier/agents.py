"""Alpha Foods' agents. They run inside the supplier workspace and only ever see Alpha Foods' data.

- Risk Agent (T1, tool: run_stress_test): detects the liquidity crisis.
- Strategy Agent (T1, tools: list_eligible_invoices, price_alternatives): picks the invoice and compares fixes.
- Seller Agent (T2, tools: evaluate_counter, submit_offer): see services/common/negotiator.py.

The numbers are computed by code (engine/); in online mode the LLM only writes the explanation.
None of these agents can sign, approve or pay: they have no such tool and no key.
"""

from __future__ import annotations

import sqlite3

from engine.stress import SCENARIOS, simulate
from services.common.llm import Backend, Tool, ToolRegistry, run_tool_loop
from services.common.negotiator import Negotiator
from services.supplier import data
from services.supplier.policy import SupplierPolicy

_OBJ = {"type": "object", "properties": {}, "required": []}


def _explain(backend: Backend | None, registry: ToolRegistry, system: str, facts: str, fallback: str) -> str:
    """Offline: the deterministic text. Online: the model may re-read via its own tools and phrase it."""
    if backend is None:
        return fallback
    out = run_tool_loop(backend, system + " Reply with the explanation text only.", facts, registry, set(),
                        max_steps=3)
    return out.text.strip() or fallback


def risk_agent(conn: sqlite3.Connection, backend: Backend | None = None) -> dict:
    profile = data.load_profile(conn)

    def run_stress_test(scenario: str) -> dict:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario}")
        return simulate(profile, scenario).summary()

    registry = ToolRegistry.of("risk_agent", [Tool(
        "run_stress_test", "Monte Carlo liquidity stress test of the company's cash over 60 days.",
        {"type": "object", "properties": {"scenario": {"type": "string", "enum": list(SCENARIOS)}},
         "required": ["scenario"]}, run_stress_test)])
    results = {s: registry.call("run_stress_test", {"scenario": s}) for s in SCENARIOS}
    normal = results["normal"]
    crisis = normal["first_critical_day"] is not None or normal["p_breach"] > data.company(conn)["alpha"]
    fallback = (f"Cash crisis in {normal['first_critical_day']} days ({normal['first_critical_date']}): "
                f"probability of falling below the reserve is {normal['p_breach']:.0%} in the normal scenario."
                if crisis else "No liquidity crisis expected in the next 60 days.")
    message = _explain(backend, registry, "You are the Risk Agent of a small company. Explain the liquidity "
                       "stress test result in two sentences.", str(results), fallback)
    return {"agent": "risk_agent", "crisis": crisis, "days_to_crisis": normal["first_critical_day"],
            "first_critical_date": normal["first_critical_date"], "scenarios": results, "message": message,
            "tool_calls": _calls(registry)}


def strategy_agent(conn: sqlite3.Connection, backend: Backend | None = None) -> dict:
    def list_eligible_invoices() -> dict:
        return {"invoices": [{k: r[k] for k in ("number", "customer", "face", "due_date")}
                             for r in data.eligible_invoices(conn)]}

    def price_alternatives(invoice_number: str) -> dict:
        p = SupplierPolicy(conn, invoice_number)
        return {"needed": round(p.needed, 2), "days": p.days, "no_action_needed": p.no_action_needed,
                "best_alternative": p.best.name if p.best else None,
                "alternatives": [a.as_dict() for a in p.alternatives]}

    registry = ToolRegistry.of("strategy_agent", [
        Tool("list_eligible_invoices", "Confirmed, undisputed, unsettled invoices.", _OBJ, list_eligible_invoices),
        Tool("price_alternatives", "Cost and feasibility of every fix for one invoice's horizon.",
             {"type": "object", "properties": {"invoice_number": {"type": "string"}},
              "required": ["invoice_number"]}, price_alternatives),
    ])
    invoices = registry.call("list_eligible_invoices", {})["invoices"]
    if not invoices:
        return {"agent": "strategy_agent", "candidate_invoice": None, "recommendation": "none_eligible",
                "message": "No eligible invoice for early payment.", "tool_calls": _calls(registry)}
    # Largest eligible invoice first: it can cover the most of the gap. Ties: earliest due date.
    candidate = sorted(invoices, key=lambda r: (-r["face"], r["due_date"]))[0]
    priced = registry.call("price_alternatives", {"invoice_number": candidate["number"]})
    if priced["no_action_needed"]:
        recommendation = "do_nothing"
        fallback = "No action needed: the stress test is within the risk limit."
    else:
        recommendation = "negotiate_early_payment"
        best = priced["best_alternative"] or "none"
        fallback = (f"Negotiate early payment of {candidate['number']} ({candidate['customer']}). It is compared "
                    f"against the best alternative without a deal: {best}.")
    message = _explain(backend, registry, "You are the Strategy Agent of a small company. Explain in two "
                       "sentences which fix to pursue and why.", str(priced), fallback)
    return {"agent": "strategy_agent", "candidate_invoice": candidate["number"], "buyer": candidate["customer"],
            "recommendation": recommendation, "needed": priced["needed"],
            "best_alternative_without_deal": priced["best_alternative"], "alternatives": priced["alternatives"],
            "message": message, "tool_calls": _calls(registry)}


def seller_agent(conn: sqlite3.Connection, invoice_number: str, backend: Backend | None = None) -> Negotiator:
    policy = SupplierPolicy(conn, invoice_number)
    return Negotiator("seller", policy.check, policy.invoice["face"], backend=backend)


def _calls(registry: ToolRegistry) -> list[dict]:
    return [{k: c[k] for k in ("tool", "args", "status")} for c in registry.calls]
