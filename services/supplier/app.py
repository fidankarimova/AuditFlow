"""Alpha Foods workspace API. Serves only Alpha Foods' own data."""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

from engine.stress import SCENARIOS, compare, simulate
from pydantic import BaseModel, Field

from services.common.llm import get_backend
from services.common.negotiator import MAX_ROUNDS
from services.common.ratelimit import EvaluationBudget, EvaluationLimitExceeded
from services.supplier import agents, data
from services.supplier.policy import SupplierPolicy

app = FastAPI(title="Alpha Foods workspace")
conn = data.connect(Path(os.environ.get("SUPPLIER_DB", data.DB_PATH)))
budget = EvaluationBudget()
_policies: dict[str, SupplierPolicy] = {}


def _policy(invoice_number: str) -> SupplierPolicy:
    if invoice_number not in _policies:
        try:
            _policies[invoice_number] = SupplierPolicy(conn, invoice_number)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
    return _policies[invoice_number]


class Offer(BaseModel):
    x: float
    d: float


class TurnRequest(BaseModel):
    negotiation_id: str
    invoice_number: str
    round: int = Field(ge=1, le=MAX_ROUNDS)
    reference: Offer
    last_offer: Offer | None = None
    history: list[dict] = []


class EvaluateRequest(BaseModel):
    negotiation_id: str
    invoice_number: str
    x: float = Field(gt=0)
    d: float = Field(gt=0, lt=1)


@app.get("/company")
def company():
    c = data.company(conn)
    return {k: c[k] for k in ("name", "as_of", "cash", "reserve")}


@app.get("/obligations")
def obligations():
    return data.obligations(conn)


@app.get("/receivables")
def receivables():
    return data.receivables(conn)


@app.get("/eligible-invoices")
def eligible_invoices():
    return data.eligible_invoices(conn)


@app.get("/forecast")
def forecast(scenario: str = "normal", bands: bool = False):
    if scenario not in SCENARIOS:
        raise HTTPException(400, f"scenario must be one of {list(SCENARIOS)}")
    r = simulate(data.load_profile(conn), scenario)
    out = r.summary()
    if bands:
        out |= {"p5": r.p5, "p50": r.p50, "p95": r.p95}
    return out


@app.get("/forecast/what-if")
def what_if(inflow_today: float, scenario: str = "normal", invoice_number: str | None = None,
            face_paid_early: float = 0.0, bands: bool = False):
    """Before / After view for an inflow today (e.g. an early payment of `face_paid_early` of an invoice, which
    then no longer arrives at its due date)."""
    if scenario not in SCENARIOS:
        raise HTTPException(400, f"scenario must be one of {list(SCENARIOS)}")
    profile = data.load_profile(conn)
    after_profile = profile.with_flow(0, inflow_today)
    if invoice_number and face_paid_early:
        inv = next((r for r in data.eligible_invoices(conn) if r["number"] == invoice_number), None)
        if inv is None:
            raise HTTPException(404, "not an eligible invoice")
        after_profile = after_profile.with_receivable_amount(invoice_number, inv["face"] - face_paid_early)
    before, after = simulate(profile, scenario), simulate(after_profile, scenario)
    out = compare(before, after)
    if bands:
        out |= {"before": {"p5": before.p5, "p50": before.p50, "p95": before.p95},
                "after": {"p5": after.p5, "p50": after.p50, "p95": after.p95}, "reserve": before.reserve}
    return out


@app.get("/negotiation-summary/{invoice_number}")
def negotiation_summary(invoice_number: str):
    """What the supplier shares to open a negotiation: no limits, no BATNA cost."""
    return _policy(invoice_number).summary()


@app.get("/alternatives/{invoice_number}")
def alternatives(invoice_number: str):
    """Supplier's own view (Strategy Agent / Supplier UI). Not sent to the buyer."""
    p = _policy(invoice_number)
    return {"needed": p.needed, "days": p.days, "alternatives": [a.as_dict() for a in p.alternatives]}


@app.post("/evaluate")
def evaluate(req: EvaluateRequest):
    try:
        budget.charge(req.negotiation_id, req.invoice_number)
    except EvaluationLimitExceeded as e:
        raise HTTPException(429, str(e)) from e
    return _policy(req.invoice_number).evaluate(req.x, req.d)


@app.get("/invoices/{invoice_number}")
def invoice(invoice_number: str):
    doc = data.invoice_document(conn, invoice_number)
    if doc is None:
        raise HTTPException(404, "unknown invoice")
    return doc


@app.post("/agents/risk/run")
def run_risk_agent():
    return agents.risk_agent(conn, get_backend())


@app.post("/agents/strategy/run")
def run_strategy_agent():
    return agents.strategy_agent(conn, get_backend())


@app.post("/agents/seller/turn")
def seller_turn(req: TurnRequest):
    try:
        negotiator = agents.seller_agent(conn, req.invoice_number, get_backend())
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    turn = req.model_dump()
    turn["invoice_memo"] = negotiator_memo(req.invoice_number)  # untrusted document text, wrapped by the agent
    return negotiator.turn(turn)


def negotiator_memo(invoice_number: str) -> str | None:
    doc = data.invoice_document(conn, invoice_number)
    return doc["memo"] if doc else None
