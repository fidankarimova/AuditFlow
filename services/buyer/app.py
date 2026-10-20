"""Beta Markets workspace API. Raw financials are private: the public surface is /evaluate."""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from engine.stress import simulate
from services.buyer import data, policy
from services.common.llm import get_backend
from services.common.negotiator import MAX_ROUNDS, Negotiator
from services.common.ratelimit import EvaluationBudget, EvaluationLimitExceeded

app = FastAPI(title="Beta Markets workspace")
conn = data.connect(Path(os.environ.get("BUYER_DB", data.DB_PATH)))
budget = EvaluationBudget()


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


@app.post("/evaluate")
def evaluate(req: EvaluateRequest):
    try:
        budget.charge(req.negotiation_id, req.invoice_number)
    except EvaluationLimitExceeded as e:
        raise HTTPException(429, str(e)) from e
    return policy.evaluate(conn, req.invoice_number, req.x, req.d)


@app.get("/internal/forecast")
def forecast(scenario: str = "normal"):
    """Buyer's own view (Buyer role in the UI). Not exposed to the coordinator."""
    return simulate(data.load_profile(conn), scenario).summary()


@app.post("/payables/check")
def check_payable(doc: dict):
    """Buyer-side check of a supplier invoice before Beta Markets' authorized human confirms it."""
    return data.check_invoice(conn, doc)


def buyer_agent(invoice_number: str) -> Negotiator:
    payable = next((p for p in data.payables(conn) if p["number"] == invoice_number), None)
    if payable is None:
        raise HTTPException(404, "unknown payable")
    return Negotiator("buyer", lambda x, d: policy.check(conn, invoice_number, x, d), payable["face"],
                      backend=get_backend())


@app.post("/agents/buyer/turn")
def buyer_turn(req: TurnRequest):
    return buyer_agent(req.invoice_number).turn(req.model_dump())
