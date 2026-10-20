"""Stochastic Liquidity Stress Engine (Monte Carlo).

Per path and day t = 1..H:  Cash_t = Cash_0 + sum(inflows up to t) - sum(outflows up to t)
- Receivables arrive at due_day + delay, delay bootstrapped from the customer's own history.
- Daily operating sales and variable expenses ~ Normal(mean, sd), floored at 0.
- Scheduled outflows (payroll, rent, ...) are fixed.

Scenarios: normal; late_payments (delays x1.5); stress (delays x2, expenses +20%).

Outputs: p_breach = P(min_t Cash_t < reserve), expected_max_shortfall = E[max(0, reserve - min_t Cash_t)],
first_critical_day = first day the median path is below reserve, and p5/p50/p95 bands.

CLI: python -m engine.stress --company alpha|beta
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta

import numpy as np

DEFAULT_HORIZON = 60
DEFAULT_PATHS = 5_000
DEFAULT_SEED = 42

# scenario -> (delay multiplier, expense multiplier)
SCENARIOS: dict[str, tuple[float, float]] = {
    "normal": (1.0, 1.0),
    "late_payments": (1.5, 1.0),
    "stress": (2.0, 1.2),
}


@dataclass(frozen=True)
class Receivable:
    label: str
    amount: float
    due_day: int  # days from as_of
    delays: tuple[float, ...]  # historical payment delays of this customer, in days


@dataclass(frozen=True)
class LiquidityProfile:
    name: str
    cash0: float
    reserve: float
    receivables: tuple[Receivable, ...] = ()
    scheduled_outflows: tuple[tuple[int, float], ...] = ()  # (day, amount)
    extra_flows: tuple[tuple[int, float], ...] = ()  # (day, signed amount); day 0 = today
    sales_mean: float = 0.0
    sales_sd: float = 0.0
    expense_mean: float = 0.0
    expense_sd: float = 0.0
    as_of: date | None = None

    def with_flow(self, day: int, amount: float) -> LiquidityProfile:
        """Adds a fixed signed cash flow (positive = inflow), e.g. an early payment."""
        return replace(self, extra_flows=self.extra_flows + ((day, amount),))

    def with_receivable_amount(self, label: str, amount: float) -> LiquidityProfile:
        """Changes the face amount still outstanding on a receivable (e.g. after partial early payment)."""
        recs = tuple(replace(r, amount=amount) if r.label == label else r for r in self.receivables)
        return replace(self, receivables=recs)

    def with_outflow_amount(self, day: int, old_amount: float, new_amount: float) -> LiquidityProfile:
        """Replaces one scheduled outflow (used on the buyer side when part of a payable is paid early)."""
        outs = list(self.scheduled_outflows)
        outs[outs.index((day, old_amount))] = (day, new_amount)
        return replace(self, scheduled_outflows=tuple(outs))


@dataclass
class StressResult:
    company: str
    scenario: str
    horizon: int
    n_paths: int
    seed: int
    reserve: float
    p_breach: float
    expected_max_shortfall: float
    first_critical_day: int | None
    first_critical_date: str | None
    p5: list[float] = field(repr=False)
    p50: list[float] = field(repr=False)
    p95: list[float] = field(repr=False)

    def summary(self) -> dict:
        d = asdict(self)
        for k in ("p5", "p50", "p95"):
            d.pop(k)
        return d


_seed_override: int | None = None


@contextmanager
def seed_override(seed: int):
    """Experiments only: run code that calls simulate() with default arguments on a different random draw
    (a fresh stress set), without changing that code."""
    global _seed_override
    previous, _seed_override = _seed_override, seed
    try:
        yield
    finally:
        _seed_override = previous


def _seed(seed: int | None) -> int:
    if seed is not None:
        return seed
    return DEFAULT_SEED if _seed_override is None else _seed_override


def simulate_paths(
    profile: LiquidityProfile,
    scenario: str = "normal",
    horizon: int = DEFAULT_HORIZON,
    n_paths: int = DEFAULT_PATHS,
    seed: int | None = None,
) -> np.ndarray:
    """Cash balance per path (rows) and day 1..H (columns)."""
    seed = _seed(seed)
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    delay_mult, expense_mult = SCENARIOS[scenario]
    rng = np.random.default_rng(seed)

    # flows[:, t] is the net flow on day t (t = 0..H); day 0 flows hit before the first check.
    flows = np.zeros((n_paths, horizon + 1))
    for rec in profile.receivables:
        if rec.amount <= 0:
            continue
        delays = rng.choice(np.asarray(rec.delays, dtype=float), size=n_paths) * delay_mult
        arrival = rec.due_day + np.rint(delays).astype(int)
        hit = arrival <= horizon
        flows[np.nonzero(hit)[0], np.maximum(arrival[hit], 0)] += rec.amount

    sales = np.maximum(rng.normal(profile.sales_mean, profile.sales_sd, (n_paths, horizon)), 0.0)
    expenses = np.maximum(
        rng.normal(profile.expense_mean * expense_mult, profile.expense_sd * expense_mult, (n_paths, horizon)), 0.0
    )
    flows[:, 1:] += sales - expenses

    for day, amount in profile.scheduled_outflows:
        if 0 <= day <= horizon:
            flows[:, day] -= amount
    for day, amount in profile.extra_flows:
        if 0 <= day <= horizon:
            flows[:, day] += amount

    return profile.cash0 + np.cumsum(flows, axis=1)[:, 1:]  # days 1..H


def simulate(
    profile: LiquidityProfile,
    scenario: str = "normal",
    horizon: int = DEFAULT_HORIZON,
    n_paths: int = DEFAULT_PATHS,
    seed: int | None = None,
) -> StressResult:
    seed = _seed(seed)
    cash = simulate_paths(profile, scenario, horizon, n_paths, seed)
    min_cash = cash.min(axis=1)
    p5, p50, p95 = np.percentile(cash, [5, 50, 95], axis=0)

    below = np.nonzero(p50 < profile.reserve)[0]
    first_day = int(below[0]) + 1 if below.size else None
    first_date = (
        (profile.as_of + timedelta(days=first_day)).isoformat() if first_day and profile.as_of else None
    )
    return StressResult(
        company=profile.name,
        scenario=scenario,
        horizon=horizon,
        n_paths=n_paths,
        seed=seed,
        reserve=profile.reserve,
        p_breach=float((min_cash < profile.reserve).mean()),
        expected_max_shortfall=float(np.maximum(profile.reserve - min_cash, 0.0).mean()),
        first_critical_day=first_day,
        first_critical_date=first_date,
        p5=p5.round(2).tolist(),
        p50=p50.round(2).tolist(),
        p95=p95.round(2).tolist(),
    )


def required_injection(profile: LiquidityProfile, alpha: float, scenario: str = "normal", **kwargs) -> float:
    """Smallest cash injection today that brings p_breach down to alpha (shortfall + reserve gap)."""
    min_cash = simulate_paths(profile, scenario, **kwargs).min(axis=1)
    return float(max(0.0, np.ceil(profile.reserve - np.quantile(min_cash, alpha))))


def run_all(profile: LiquidityProfile, **kwargs) -> dict[str, StressResult]:
    return {s: simulate(profile, s, **kwargs) for s in SCENARIOS}


def compare(before: StressResult, after: StressResult) -> dict:
    """Before / After action view."""
    return {
        "scenario": before.scenario,
        "p_breach_before": before.p_breach,
        "p_breach_after": after.p_breach,
        "p_breach_change": after.p_breach - before.p_breach,
        "expected_max_shortfall_before": before.expected_max_shortfall,
        "expected_max_shortfall_after": after.expected_max_shortfall,
        "first_critical_day_before": before.first_critical_day,
        "first_critical_day_after": after.first_critical_day,
    }


def _load_profile(company: str) -> LiquidityProfile:
    # Each workspace builds its own profile from its own DB; imported lazily so the engine stays generic.
    if company == "alpha":
        from services.supplier import data
    elif company == "beta":
        from services.buyer import data
    else:
        raise SystemExit("--company must be alpha or beta")
    return data.load_profile(data.connect())


def main() -> None:
    parser = argparse.ArgumentParser(description="Monte Carlo liquidity stress test")
    parser.add_argument("--company", required=True, choices=["alpha", "beta"])
    parser.add_argument("--paths", type=int, default=DEFAULT_PATHS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    profile = _load_profile(args.company)
    print(f"{profile.name}: cash {profile.cash0:,.0f}, reserve {profile.reserve:,.0f}, as of {profile.as_of}")
    print(f"{'scenario':<14} {'p_breach':>9} {'E[max shortfall]':>17} {'first critical':>15}")
    for name, r in run_all(profile, n_paths=args.paths, seed=args.seed).items():
        critical = f"day {r.first_critical_day}" if r.first_critical_day else "-"
        print(f"{name:<14} {r.p_breach:>9.1%} {r.expected_max_shortfall:>17,.0f} {critical:>15}")


if __name__ == "__main__":
    main()
