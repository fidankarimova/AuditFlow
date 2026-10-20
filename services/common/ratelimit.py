"""Per-negotiation evaluation budget, enforced by the workspace that owns the private data.

Limits probing of private limits: at most MAX_EVALUATIONS evaluate() calls per negotiation, and at most
MAX_NEGOTIATIONS_PER_INVOICE distinct negotiations per invoice. Repeated negotiations can still leak coarse
information (documented known risk); this only bounds it.
"""

MAX_EVALUATIONS = 200
MAX_NEGOTIATIONS_PER_INVOICE = 3


class EvaluationLimitExceeded(Exception):
    pass


class EvaluationBudget:
    def __init__(self, max_calls: int = MAX_EVALUATIONS, max_negotiations: int = MAX_NEGOTIATIONS_PER_INVOICE):
        self.max_calls = max_calls
        self.max_negotiations = max_negotiations
        self.calls: dict[str, int] = {}
        self.negotiations: dict[str, set[str]] = {}

    def charge(self, negotiation_id: str, invoice_number: str | None = None) -> int:
        if invoice_number is not None:
            seen = self.negotiations.setdefault(invoice_number, set())
            if negotiation_id not in seen and len(seen) >= self.max_negotiations:
                raise EvaluationLimitExceeded(
                    f"{invoice_number}: limit of {self.max_negotiations} negotiations reached")
            seen.add(negotiation_id)
        used = self.calls.get(negotiation_id, 0)
        if used >= self.max_calls:
            raise EvaluationLimitExceeded(f"{negotiation_id}: limit of {self.max_calls} evaluations reached")
        self.calls[negotiation_id] = used + 1
        return used + 1
