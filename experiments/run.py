"""Experiments: BILNO vs three baselines on 200 seeded synthetic scenarios (Phase 10).

Models (each decides with its OWN simulation, i.e. the production default seed):
- B1  fixed 2% discount on the full invoice whenever the supplier's stress test says action is needed.
- B2  supplier-only optimizer: cheapest early payment that fixes the supplier's own risk; ignores the buyer.
- B3  both gains (Nash product, both private limits) on a deterministic forecast: no stress test on either side.
- BILNO  the production decision (engine.bilno.decide) over the production policy code.

Every outcome is then evaluated on a SEPARATE, freshly drawn stress set (different seed): crisis avoided, surplus,
costs, buyer reserve breach, agreement feasibility and no-deal correctness against an oracle that runs the same
search on the evaluation draw.

The policy code is the production code: each scenario is written into in-memory SQLite databases with the
supplier/buyer workspace schemas, and services.supplier.policy / services.buyer.policy answer evaluate(x, d).

Run: python -m experiments.run [--n 200] [--workers 8]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from multiprocessing import Pool

import numpy as np

from engine import bilno
from engine.alternatives import bank_loan_cost
from engine.stress import LiquidityProfile, Receivable, seed_override, simulate
from services.buyer import data as buyer_data
from services.buyer import policy as buyer_policy
from services.common.config import REPO_ROOT
from services.supplier import data as supplier_data
from services.supplier.policy import SupplierPolicy

SCENARIO_SEED = 2026
EVAL_SEED = 777_001  # fresh stress set; the models use the engine default seed (42)
AS_OF = date(2026, 10, 20)
INVOICE = "INV-1"
MODELS = ("B1", "B2", "B3", "BILNO")
OUT_JSON = REPO_ROOT / "experiments" / "results.json"
OUT_DOC = REPO_ROOT / "docs" / "EXPERIMENTS.md"
OUT_PNG = REPO_ROOT / "docs" / "experiments_chart.png"


# ----------------------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    sid: int
    regime: str
    s_cash: float
    s_reserve: float
    s_sales: float
    s_expense: float
    payroll: float
    payroll_day: int
    rent: float
    rent_day: int
    rent_shift_fee: float
    face: float
    due_day: int
    buyer_delays: list[float]
    other_face: float
    other_due: int
    other_delays: list[float]
    bank_rate: float
    bank_arrival: int
    d_max: float
    b_cash: float
    b_reserve: float
    b_sales: float
    b_expense: float
    b_outflows: list[tuple[int, float]]
    r_opp: float
    max_early_cash: float


def generate(n: int, seed: int = SCENARIO_SEED) -> list[Scenario]:
    """Mix of regimes so that some scenarios need no action and some should end without a deal."""
    rng = np.random.default_rng(seed)
    regimes = rng.choice(["crisis", "no_need", "tight_buyer", "costly_buyer"], size=n, p=[0.5, 0.2, 0.15, 0.15])
    out = []
    for i, regime in enumerate(regimes):
        face = float(rng.integers(40, 201) * 1_000)
        payroll = float(rng.integers(30, 121) * 1_000)
        s_cash = float(rng.integers(5, 40) * 1_000)
        if regime == "no_need":
            s_cash = payroll + float(rng.integers(40, 120) * 1_000)
        b_cash = float(rng.integers(150, 801) * 1_000)
        b_reserve = b_cash * float(rng.uniform(0.25, 0.6))
        r_opp = float(rng.uniform(0.04, 0.14))
        if regime == "tight_buyer":
            b_reserve = b_cash * float(rng.uniform(0.85, 0.97))
        if regime == "costly_buyer":
            r_opp = float(rng.uniform(0.25, 0.45))
        s_sales = float(rng.uniform(1_000, 4_000))
        b_sales = b_cash * float(rng.uniform(0.04, 0.08))
        out.append(Scenario(
            sid=i, regime=str(regime), s_cash=s_cash, s_reserve=float(rng.integers(5, 21) * 1_000),
            s_sales=s_sales, s_expense=s_sales * float(rng.uniform(0.5, 0.9)),
            payroll=payroll, payroll_day=int(rng.integers(5, 21)),
            rent=float(rng.integers(3, 16) * 1_000), rent_day=int(rng.integers(10, 31)),
            rent_shift_fee=float(rng.integers(2, 10) * 100),
            face=face, due_day=int(rng.integers(30, 61)),
            buyer_delays=[float(x) for x in rng.choice([0, 0, 1, 2, 3, 5], size=10)],
            other_face=float(rng.integers(5, 40) * 1_000), other_due=int(rng.integers(15, 45)),
            other_delays=[float(x) for x in rng.integers(0, 40, size=10)],
            bank_rate=float(rng.uniform(0.14, 0.30)), bank_arrival=int(rng.integers(2, 9)),
            d_max=float(rng.choice([0.02, 0.025, 0.03, 0.035, 0.04])),
            b_cash=b_cash, b_reserve=b_reserve, b_sales=b_sales, b_expense=b_sales * float(rng.uniform(0.8, 1.0)),
            b_outflows=[(int(rng.integers(5, 55)), float(b_cash * rng.uniform(0.05, 0.15))) for _ in range(3)],
            r_opp=r_opp, max_early_cash=face * float(rng.uniform(0.4, 1.3)),
        ))
    return out


def _iso(day: int) -> str:
    return (AS_OF + timedelta(days=day)).isoformat()


def supplier_db(sc: Scenario) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(supplier_data.SCHEMA)
    conn.execute("INSERT INTO company VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("Supplier", AS_OF.isoformat(), sc.s_cash, sc.s_reserve, sc.s_sales, sc.s_sales * 0.25,
                  sc.s_expense, sc.s_expense * 0.25, sc.bank_rate, sc.bank_arrival, 500, sc.d_max, 0.05, 5_000))
    conn.executemany("INSERT INTO receivables (number, customer, face, due_date, buyer_confirmed, status, memo) "
                     "VALUES (?,?,?,?,?,?,?)",
                     [(INVOICE, "Buyer", sc.face, _iso(sc.due_day), 1, "open", ""),
                      ("INV-2", "Other", sc.other_face, _iso(sc.other_due), 1, "open", "")])
    conn.executemany("INSERT INTO delay_history VALUES (?,?)",
                     [("Buyer", d) for d in sc.buyer_delays] + [("Other", d) for d in sc.other_delays])
    conn.executemany("INSERT INTO scheduled_outflows VALUES (?,?,?,?,?)",
                     [(_iso(sc.payroll_day), sc.payroll, "payroll", None, None),
                      (_iso(sc.rent_day), sc.rent, "rent", 15, sc.rent_shift_fee)])
    return conn


def buyer_db(sc: Scenario) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(buyer_data.SCHEMA)
    conn.execute("INSERT INTO company VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("Buyer", AS_OF.isoformat(), sc.b_cash, sc.b_reserve, sc.b_sales, sc.b_sales * 0.2, sc.b_expense,
                  sc.b_expense * 0.2, sc.r_opp, sc.max_early_cash, 0.02))
    conn.execute("INSERT INTO payables (number, supplier, face, due_date) VALUES (?,?,?,?)",
                 (INVOICE, "Supplier", sc.face, _iso(sc.due_day)))
    conn.executemany("INSERT INTO scheduled_outflows VALUES (?,?,?)",
                     [(_iso(day), amount, "other") for day, amount in sc.b_outflows])
    return conn


# ----------------------------------------------------------------------------- models


@dataclass
class Action:
    deal: tuple[float, float] | None  # (x, d)
    loan: float = 0.0  # bank loan amount arriving after bank_arrival days
    shift: bool = False  # rent moved by its allowed shift
    note: str = ""


def _fallback(sp: SupplierPolicy, note: str) -> Action:
    """The supplier's best alternative without a deal (its BATNA)."""
    best = sp.best
    if best is None:
        return Action(None, note=f"{note}; no feasible alternative")
    if best.name == "bank_loan":
        return Action(None, loan=sp.needed, note=f"{note}; bank loan")
    if best.name == "date_shift":
        return Action(None, shift=True, note=f"{note}; date shift")
    return Action(None, note=f"{note}; do nothing")


def model_b1(sp: SupplierPolicy, sc: Scenario, conn_b) -> Action:
    if sp.no_action_needed:
        return Action(None, note="no action needed")
    x, d = sc.face, 0.02
    return Action((x, d), loan=max(0.0, sp.needed - x * (1 - d)), note="fixed 2% on the full invoice")


def model_b2(sp: SupplierPolicy, sc: Scenario, conn_b) -> Action:
    """Cheapest early payment for the supplier under its own limits and risk test; the buyer is not consulted."""
    if sp.no_action_needed:
        return Action(None, note="no action needed")
    d = 0.001  # the supplier's cost x*d grows with d, so the smallest discount is always cheapest
    x0 = min(sc.face, math.ceil(sp.needed / (1 - d) / 1_000) * 1_000)
    for x in np.arange(max(1_000.0, x0), sc.face + 1, 1_000.0):
        if sp.check(float(x), d)["ok"]:
            return Action((float(x), d), note="supplier cost minimum")
    return _fallback(sp, "no supplier-feasible early payment")


def _deterministic(profile: LiquidityProfile) -> LiquidityProfile:
    recs = tuple(Receivable(r.label, r.amount, r.due_day, (float(np.mean(r.delays)),)) for r in profile.receivables)
    return LiquidityProfile(profile.name, profile.cash0, profile.reserve, recs, profile.scheduled_outflows,
                            profile.extra_flows, profile.sales_mean, 0.0, profile.expense_mean, 0.0, profile.as_of)


def model_b3(sp: SupplierPolicy, sc: Scenario, conn_b) -> Action:
    """Nash product of both gains under both sides' private limits, on a deterministic (mean) forecast."""
    det = _deterministic(sp.profile)
    path = simulate(det, n_paths=1).p50
    needed = max(0.0, math.ceil(sc.s_reserve - min(path)))
    if needed == 0:
        return Action(None, note="deterministic forecast: no action needed")
    days, bank = sp.days, sp.bank
    batna = bank_loan_cost(needed, bank, days)
    company = buyer_data.company(conn_b)

    def s_eval(x, d):
        if not (0 < d <= sc.d_max) or x * (1 - d) < 5_000:
            return {"feasible": False, "gain_bucket": 0}
        gain = batna - (x * d + bank_loan_cost(max(0.0, needed - x * (1 - d)), bank, days))
        return {"feasible": gain > 0, "gain_bucket": int(round(gain / 100)) if gain > 0 else 0}

    def b_eval(x, d):
        gain = buyer_policy.buyer_gain(x, d, company["opportunity_cost"], days)
        ok = x * (1 - d) <= company["max_early_cash"] and gain > 0
        return {"feasible": ok, "gain_bucket": int(round(gain / 100)) if ok else 0}

    r = bilno.optimize(s_eval, b_eval, sc.face, days)
    if r.status != bilno.AGREEMENT:
        return Action(None, loan=needed, note="no deal on deterministic forecast; bank loan")
    return Action((r.x, r.d), loan=max(0.0, needed - r.x * (1 - r.d)), note="deterministic Nash optimum")


def model_bilno(sp: SupplierPolicy, sc: Scenario, conn_b) -> Action:
    r = bilno.decide(sp.summary(), sp.evaluate, lambda x, d: buyer_policy.evaluate(conn_b, INVOICE, x, d))
    if r.status == bilno.AGREEMENT:
        return Action((r.x, r.d), loan=max(0.0, sp.needed - r.x * (1 - r.d)), note="BILNO agreement")
    if r.status == bilno.NO_ACTION_NEEDED:
        return Action(None, note="no action needed")
    return _fallback(sp, "NO_FEASIBLE_AGREEMENT")


MODEL_FNS = {"B1": model_b1, "B2": model_b2, "B3": model_b3, "BILNO": model_bilno}


# ----------------------------------------------------------------------------- evaluation


def _apply_supplier(sp: SupplierPolicy, sc: Scenario, a: Action) -> LiquidityProfile:
    p = sp.profile
    if a.deal:
        x, d = a.deal
        p = p.with_flow(0, x * (1 - d)).with_receivable_amount(INVOICE, sc.face - x)
    if a.loan > 0:
        p = p.with_flow(sc.bank_arrival, a.loan)
    if a.shift:
        p = p.with_flow(sc.rent_day, sc.rent).with_flow(sc.rent_day + 15, -sc.rent)
    return p


def evaluate_scenario(sc: Scenario) -> dict:
    t0 = time.time()
    conn_s, conn_b = supplier_db(sc), buyer_db(sc)
    sp = SupplierPolicy(conn_s, INVOICE)  # the models' view (default seed)
    actions = {m: fn(sp, sc, conn_b) for m, fn in MODEL_FNS.items()}

    with seed_override(EVAL_SEED):  # fresh stress set for evaluation and the oracle
        sp_e = SupplierPolicy(conn_s, INVOICE)
        s_pre = simulate(sp_e.profile).p_breach
        crisis = s_pre > 0.05
        b_profile = buyer_data.load_profile(conn_b)
        b_pre = simulate(b_profile).p_breach
        oracle = bilno.decide(sp_e.summary(), sp_e.evaluate, lambda x, d: buyer_policy.evaluate(conn_b, INVOICE, x, d))
        should_deal = oracle.status == bilno.AGREEMENT
        need = sp_e.needed  # cash the supplier actually needs, measured on the evaluation draw

        def outcome(a: Action) -> dict:
            after = simulate(_apply_supplier(sp_e, sc, a))
            fin = bank_loan_cost(a.loan, sp_e.bank, sp_e.days) + (sc.rent_shift_fee if a.shift else 0.0)
            received = a.loan
            if a.deal:
                fin += a.deal[0] * a.deal[1]
                received += a.deal[0] * (1 - a.deal[1])
            excess = max(0.0, received - need) / received if received else 0.0
            # Shortfall left after the action is valued 1:1 as a cost (an underestimate of a missed payroll).
            return {"p": after.p_breach, "financing": fin, "total": fin + after.expected_max_shortfall,
                    "unnecessary": fin * excess}

        batna = outcome(_fallback(sp_e, "evaluation BATNA") if not sp_e.no_action_needed else Action(None))
        avoidable = crisis and (should_deal or batna["p"] <= 0.05)
        rows = {}
        for m, a in actions.items():
            o = outcome(a)
            buyer_gain, b_post, feasible, failed = 0.0, b_pre, None, []
            if a.deal:
                x, d = a.deal
                buyer_gain = buyer_policy.buyer_gain(x, d, sc.r_opp, sp_e.days)
                b_after = b_profile.with_flow(0, -x * (1 - d)).with_outflow_amount(sc.due_day, sc.face, sc.face - x)
                b_post = simulate(b_after).p_breach
                failed = ([f"supplier:{f}" for f in sp_e.check(x, d)["failed"]]
                          + [f"buyer:{f}" for f in buyer_policy.check(conn_b, INVOICE, x, d)["failed"]])
                feasible = not failed
            rows[m] = {"deal": a.deal, "loan": round(a.loan), "shift": a.shift, "note": a.note,
                       "s_post": o["p"], "crisis_avoided": (o["p"] <= 0.05) if crisis else None,
                       "financing_cost": round(o["financing"], 2), "unnecessary_cost": round(o["unnecessary"], 2),
                       "supplier_gain": round(batna["total"] - o["total"], 2),
                       "buyer_gain": round(buyer_gain, 2), "b_post": b_post,
                       "buyer_breach": bool(a.deal) and b_post > 0.02 and b_post > b_pre,
                       "feasible_for_both": feasible, "failed_limits": failed}
    return {"sid": sc.sid, "regime": sc.regime, "crisis": crisis, "s_pre": s_pre, "b_pre": b_pre,
            "should_deal": should_deal, "avoidable": avoidable, "oracle": oracle.as_dict(),
            "need": round(need, 2), "batna_total_cost": round(batna["total"], 2),
            "models": rows, "seconds": round(time.time() - t0, 2)}


# ----------------------------------------------------------------------------- metrics


def metrics(results: list[dict]) -> dict[str, dict]:
    out = {}
    crisis = [r for r in results if r["crisis"]]
    no_need = [r for r in results if not r["crisis"]]
    avoidable = [r for r in results if r["avoidable"]]
    no_deal_should = [r for r in results if not r["should_deal"]]
    should = [r for r in results if r["should_deal"]]
    for m in MODELS:
        rows = [r["models"][m] for r in results]
        deals = [x for x in rows if x["deal"]]
        out[m] = {
            "crisis_avoidance_rate": _mean([r["models"][m]["crisis_avoided"] for r in crisis]),
            "avoidable_crisis_avoidance_rate": _mean([r["models"][m]["crisis_avoided"] for r in avoidable]),
            "crisis_avoidance_rate_tol": _mean([r["models"][m]["s_post"] <= 0.075 for r in crisis]),
            "mean_p_breach_after_crisis": _mean([r["models"][m]["s_post"] for r in crisis]),
            "joint_surplus_total": sum(x["supplier_gain"] + x["buyer_gain"] for x in rows),
            "joint_surplus_mean": _mean([x["supplier_gain"] + x["buyer_gain"] for x in rows]),
            "joint_surplus_feasible_deals": sum(x["supplier_gain"] + x["buyer_gain"] for x in deals
                                                if x["feasible_for_both"]),
            "joint_surplus_infeasible_deals": sum(x["supplier_gain"] + x["buyer_gain"] for x in deals
                                                  if not x["feasible_for_both"]),
            "supplier_gain_total": sum(x["supplier_gain"] for x in rows),
            "buyer_gain_total": sum(x["buyer_gain"] for x in rows),
            "unnecessary_financing_cost_mean": _mean([x["unnecessary_cost"] for x in rows]),
            "financing_cost_mean": _mean([x["financing_cost"] for x in rows]),
            "buyer_reserve_breach_rate": _mean([x["buyer_breach"] for x in deals]) if deals else 0.0,
            "buyer_reserve_breaches": sum(x["buyer_breach"] for x in deals),
            "buyer_loss_deals": sum(1 for x in deals if x["buyer_gain"] <= 0),
            "deals": len(deals),
            "feasible_agreement_rate": _mean([x["feasible_for_both"] for x in deals]) if deals else None,
            "no_deal_correctness": _mean([not r["models"][m]["deal"] for r in no_deal_should]),
            "failed_limits": _count([f for x in deals for f in x["failed_limits"]]),
            "deal_recall": _mean([bool(r["models"][m]["deal"]) and r["models"][m]["feasible_for_both"]
                                  for r in should]),
        }
    out["_counts"] = {"scenarios": len(results), "crisis": len(crisis), "no_need": len(no_need),
                      "avoidable": len(avoidable),
                      "should_deal": len(should), "should_not_deal": len(no_deal_should)}
    return out


def _count(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def execution_integrity() -> dict:
    """Runs the named security tests; this is a property of the AuditFlow system, not of an optimizer."""
    names = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "unlisted_tool", "both_approvals",
             "tamper"]
    expr = " or ".join(names)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-k", expr, "-p", "no:cacheprovider",
                        "tests/test_phase6.py", "tests/test_phase7.py"], cwd=REPO_ROOT, capture_output=True,
                       text=True)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]
    passed = int(last.split(" passed")[0].split()[-1]) if " passed" in last else 0
    failed = int(last.split(" failed")[0].split()[-1]) if " failed" in last else 0
    return {"passed": passed, "failed": failed, "summary": last}


# ----------------------------------------------------------------------------- report


def _pct(v) -> str:
    return "-" if v is None else f"{v:.1%}"


def chart(m: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"B1": "#2a78d6", "B2": "#eb6834", "B3": "#1baf7a", "BILNO": "#6250d6"}  # categorical slots 1, 2, 3, 7
    panels = [("Crisis avoidance (P(breach) ≤ 5%)", "crisis_avoidance_rate", True),
              ("Crisis avoidance (≤ 7.5% tolerance)", "crisis_avoidance_rate_tol", True),
              ("Mean P(breach) after action, crises ↓", "mean_p_breach_after_crisis", True),
              ("No-deal correctness", "no_deal_correctness", True),
              ("Feasible agreement rate (of deals made)", "feasible_agreement_rate", True),
              ("Buyer reserve breach rate (of deals)", "buyer_reserve_breach_rate", True),
              ("Joint economic surplus (TL, total)", "joint_surplus_total", False),
              ("Deals where the buyer loses money ↓", "buyer_loss_deals", False),
              ("Unnecessary financing cost (TL / scenario)", "unnecessary_financing_cost_mean", False)]
    fig, axes = plt.subplots(3, 3, figsize=(15, 11))
    for ax, (title, key, pct) in zip(axes.flat, panels):
        vals = [m[k][key] or 0 for k in MODELS]
        bars = ax.bar(MODELS, vals, color=[colors[k] for k in MODELS], width=0.6, edgecolor="white", linewidth=2)
        ax.set_title(title, fontsize=10, loc="left", color="#0b0b0b")
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#c3c2b7")
        ax.tick_params(colors="#52514e", labelsize=9)
        ax.yaxis.grid(True, color="#e1e0d9", linewidth=0.8)
        ax.set_axisbelow(True)
        if pct:
            ax.set_ylim(0, 1.05)
            ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        for b, v in zip(bars, vals):
            label = f"{v:.0%}" if pct else f"{v:,.0f}"
            ax.annotate(label, (b.get_x() + b.get_width() / 2, v), ha="center", va="bottom" if v >= 0 else "top",
                        fontsize=8, color="#52514e", xytext=(0, 2 if v >= 0 else -2), textcoords="offset points")
        if not pct:
            ax.axhline(0, color="#c3c2b7", linewidth=1)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    fig.suptitle(f"BILNO vs baselines on {m['_counts']['scenarios']} synthetic scenarios "
                 "(evaluated on a separate stress draw)", fontsize=12, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=130)


_RESULTS: list[dict] = []


def write_doc(results: list[dict], m: dict, integrity: dict, elapsed: float) -> None:
    _RESULTS[:] = results
    c = m["_counts"]
    rows = [
        ("Liquidity Crisis Avoidance Rate ↑", "crisis_avoidance_rate", _pct, f"of {c['crisis']} crisis scenarios"),
        ("  among avoidable crises ↑", "avoidable_crisis_avoidance_rate", _pct,
         f"of {c['avoidable']} crises that some action (deal or alternative) can avoid"),
        ("  with a 7.5% tolerance ↑", "crisis_avoidance_rate_tol", _pct,
         "P(breach) after ≤ 7.5%: separates near-misses at the 5% limit from unsolved crises"),
        ("Mean P(cash < reserve) after action, crises ↓", "mean_p_breach_after_crisis", _pct, "evaluation draw"),
        ("Joint Economic Surplus (total TL) ↑", "joint_surplus_total", lambda v: f"{v:,.0f}", "supplier + buyer gain"),
        ("  from deals within both sides' limits (TL)", "joint_surplus_feasible_deals", lambda v: f"{v:,.0f}",
         "surplus a buyer that checks its own limits would actually agree to"),
        ("  from deals that break a limit (TL)", "joint_surplus_infeasible_deals", lambda v: f"{v:,.0f}", ""),
        ("  of which supplier gain (TL)", "supplier_gain_total", lambda v: f"{v:,.0f}", "vs its best alternative"),
        ("  of which buyer gain (TL)", "buyer_gain_total", lambda v: f"{v:,.0f}", "vs paying at due date"),
        ("Unnecessary Financing Cost (TL / scenario) ↓", "unnecessary_financing_cost_mean", lambda v: f"{v:,.0f}",
         "financing cost on cash raised beyond the need (evaluation draw)"),
        ("Total financing cost (TL / scenario)", "financing_cost_mean", lambda v: f"{v:,.0f}", "discount + loan + fees"),
        ("Buyer Reserve Breach Rate ↓", "buyer_reserve_breach_rate", _pct,
         "share of deals that leave the buyer above its 2% risk limit and make it worse"),
        ("Deals where the buyer loses money ↓", "buyer_loss_deals", str, "count"),
        ("Deals made", "deals", str, "count"),
        ("Feasible Agreement Rate ↑", "feasible_agreement_rate", _pct, "share of deals within BOTH sides' limits"),
        ("No-Deal Correctness ↑", "no_deal_correctness", _pct, f"of {c['should_not_deal']} scenarios where no deal should be made"),
        ("Deal recall ↑", "deal_recall", _pct, f"feasible deals made, of {c['should_deal']} scenarios where one exists"),
    ]
    table = ["| Metric | B1 fixed 2% | B2 supplier-only | B3 no stress test | **BILNO** | Note |", "|---|---|---|---|---|---|"]
    for label, key, fmt, note in rows:
        cells = [fmt(m[k][key]) if m[k][key] is not None else "-" for k in MODELS]
        table.append(f"| {label} | {cells[0]} | {cells[1]} | {cells[2]} | **{cells[3]}** | {note} |")
    table.append(f"| Execution Integrity | n/a | n/a | n/a | **{integrity['passed']}/{integrity['passed'] + integrity['failed']}"
                 f" security tests pass** | system property (S1–S10, unlisted tool, both approvals, audit tamper); "
                 f"baselines are optimizers without an execution layer |")

    regimes = {}
    for r in results:
        regimes.setdefault(r["regime"], []).append(r)
    reg_rows = ["| Regime | Scenarios | Crisis | Should deal | BILNO deals | B1 deals | BILNO crisis avoided | B1 crisis avoided |",
                "|---|---|---|---|---|---|---|---|"]
    for name, rs in sorted(regimes.items()):
        cr = [r for r in rs if r["crisis"]]
        reg_rows.append(
            f"| {name} | {len(rs)} | {len(cr)} | {sum(r['should_deal'] for r in rs)} | "
            f"{sum(bool(r['models']['BILNO']['deal']) for r in rs)} | {sum(bool(r['models']['B1']['deal']) for r in rs)} | "
            f"{_pct(_mean([r['models']['BILNO']['crisis_avoided'] for r in cr]))} | "
            f"{_pct(_mean([r['models']['B1']['crisis_avoided'] for r in cr]))} |")

    best = {key: max(MODELS, key=lambda k: (m[k][key] if m[k][key] is not None else -1e18))
            for key in ("crisis_avoidance_rate", "joint_surplus_total", "no_deal_correctness", "feasible_agreement_rate")}
    doc = f"""# Experiments: BILNO vs baselines

Generated by `python -m experiments.run` (scenario seed {SCENARIO_SEED}, evaluation seed {EVAL_SEED},
{c['scenarios']} scenarios, {elapsed / 60:.1f} min). Raw per-scenario results: `experiments/results.json`.
All numbers below come from that run. All amounts are synthetic example values.

![chart](experiments_chart.png)

## Setup

- **Scenarios.** {c['scenarios']} synthetic supplier/buyer pairs drawn from four regimes: `crisis` (50%),
  `no_need` (20%, the supplier has enough cash), `tight_buyer` (15%, the buyer's reserve is close to its cash) and
  `costly_buyer` (15%, the buyer's opportunity cost is 25–45%). Within each regime, balances, payroll, invoice size
  and due date, customer delay histories, bank terms, the supplier's max discount, and the buyer's reserve,
  opportunity cost, max early cash and other outflows are all random.
- **Production code.** Each scenario is written into in-memory SQLite databases with the real workspace schemas.
  The supplier and buyer checks are `services.supplier.policy.SupplierPolicy` and `services.buyer.policy`, and
  BILNO is `engine.bilno.decide`.
- **Models.** Each model decides with its own simulation (the engine's default seed):
  - **B1**: fixed 2% discount on the full invoice whenever the supplier's stress test says action is needed.
  - **B2**: the cheapest early payment for the supplier (smallest discount, smallest amount that passes the
    supplier's own risk test). It ignores the buyer.
  - **B3**: maximizes the Nash product of both gains under both sides' private limits (max discount, min net amount,
    max early cash), using a deterministic mean forecast. Neither side runs a stress test.
  - **BILNO**: the production decision, which includes both stress tests.

  When a model makes no deal, the supplier uses its best alternative without a deal (e.g. the bank loan) where the
  model has one.
- **Evaluation.** Every outcome is scored on a separate, freshly drawn stress set (seed {EVAL_SEED}, 5,000
  paths), not on the model's own simulation. "Should deal" comes from an oracle: the same BILNO search, run on the
  evaluation draw.
- **Metric definitions.**
  - *Crisis*: the supplier's P(cash < reserve) before any action is > 5%. It is *avoided* if it is ≤ 5% after the
    model's action.
  - *Surplus*: the supplier's saving vs its best alternative, plus the buyer's gain `x·d − x(1−d)·r_opp·D/365`.
    Each supplier outcome costs its financing cost plus the expected shortfall left after it, with shortfall valued
    1:1 (an underestimate of what a missed payroll costs).
  - *Unnecessary financing cost*: the part of the financing cost spent on cash raised beyond what the supplier
    needed on the evaluation draw.
  - *Buyer reserve breach*: after the deal, the buyer's P(reserve breach) is above its 2% limit and higher than
    without the deal.

## Results

{chr(10).join(table)}

## By regime

{chr(10).join(reg_rows)}

## Reading the results (including where BILNO is not better)

- Best per metric in this run: crisis avoidance **{best['crisis_avoidance_rate']}**, joint surplus
  **{best['joint_surplus_total']}**, no-deal correctness **{best['no_deal_correctness']}**, feasible agreements
  **{best['feasible_agreement_rate']}**.
{_observations(m)}

## Limitations

- The scenarios are synthetic, and the regimes were chosen by us. The real-world mix of cases is unknown.
- B3's "no stress test" uses a deterministic mean forecast. Other ways to drop the stress test would give other
  numbers.
- The oracle uses the same grid search as BILNO, on the evaluation draw. A feasible point between grid steps could
  be missed.
- BILNO's evaluation draw differs from its own simulation only in the random seed, not in the model. A wrong delay
  model would hurt BILNO and the oracle alike.
- Execution integrity is measured by our own security tests, not by an external audit.
"""
    OUT_DOC.write_text(doc)


def _under_raised(model: str) -> str:
    deals = [r for r in _RESULTS if r["crisis"] and r["models"][model]["deal"]]
    under = sum(1 for r in deals
                if r["models"][model]["deal"][0] * (1 - r["models"][model]["deal"][1]) + r["models"][model]["loan"]
                < r["need"])
    return f"{under} of {len(deals)}"


def _observations(m: dict) -> str:
    lines = []
    b, b1, b2, b3 = m["BILNO"], m["B1"], m["B2"], m["B3"]
    lines.append(f"- BILNO avoided {_pct(b['crisis_avoidance_rate'])} of crises vs B1 {_pct(b1['crisis_avoidance_rate'])}, "
                 f"B2 {_pct(b2['crisis_avoidance_rate'])}, B3 {_pct(b3['crisis_avoidance_rate'])}.")
    if b1["crisis_avoidance_rate"] and b1["crisis_avoidance_rate"] >= b["crisis_avoidance_rate"]:
        lines.append("  B1 avoids more crises because it always takes the full invoice at 2%. It gets there "
                     f"by ignoring the buyer: {b1['buyer_loss_deals']} of its {b1['deals']} deals lose the buyer money "
                     f"and {b1['buyer_reserve_breaches']} push the buyer over its risk limit.")
    edge = [r for r in _RESULTS if r["crisis"] and r["models"]["BILNO"]["deal"]]
    near = sum(1 for r in edge if 0.05 < r["models"]["BILNO"]["s_post"] <= 0.075)
    far = sum(1 for r in edge if r["models"]["BILNO"]["s_post"] > 0.075)
    lines.append(f"- **Edge-of-limit effect.** BILNO picks the cheapest terms that just meet the supplier's 5% risk limit "
                 f"on its own simulation. On the fresh draw, {near} of its {len(edge)} crisis deals land between 5% and "
                 f"7.5% and {far} land above 7.5%. Its bank-loan fallback is sized the same way. The strict 5% rate "
                 f"therefore understates it: with a 7.5% tolerance BILNO reaches "
                 f"{_pct(b['crisis_avoidance_rate_tol'])}. A production version should target a safety margin below "
                 "the limit (not implemented).")
    lines.append("- **Partial relief is refused.** BILNO only accepts terms that bring the supplier within its risk "
                 "limit. When no single invoice can do that, it makes no deal, even if a deal would cut the expected "
                 "shortfall a lot. B3 makes such deals, and because its deterministic forecast underestimates the "
                 f"need, it also sizes them too small ({_under_raised('B3')} of its crisis deals raise less cash than "
                 "the stress-tested need). Its mean P(breach) after action is "
                 f"{_pct(b3['mean_p_breach_after_crisis'])} vs BILNO {_pct(b['mean_p_breach_after_crisis'])}. Its "
                 "surplus still counts the partial shortfall reduction, valued 1:1. "
                 f"Without a buyer stress test, {b3['buyer_reserve_breaches']} of its {b3['deals']} deals leave the "
                 "buyer above its risk limit.")
    lines.append(f"- Deals that lose the buyer money: B1 {b1['buyer_loss_deals']}, B2 {b2['buyer_loss_deals']}, "
                 f"B3 {b3['buyer_loss_deals']}, BILNO {b['buyer_loss_deals']}. "
                 f"Buyer reserve breaches: B1 {b1['buyer_reserve_breaches']}, B2 {b2['buyer_reserve_breaches']}, "
                 f"B3 {b3['buyer_reserve_breaches']}, BILNO {b['buyer_reserve_breaches']}.")
    if b2["joint_surplus_total"] > b["joint_surplus_total"] or b3["joint_surplus_total"] > b["joint_surplus_total"]:
        shares = ", ".join(
            f"{k} {m[k]['joint_surplus_infeasible_deals'] / m[k]['joint_surplus_total']:.0%}"
            for k in ("B1", "B2", "B3") if m[k]["joint_surplus_total"] > 0)
        feas = sorted(MODELS, key=lambda k: -m[k]["joint_surplus_feasible_deals"])
        lines.append("- **BILNO is not the surplus maximum here.** B1, B2 and B3 all show a higher total joint surplus. "
                     f"The share of each total that comes from deals breaking a side's limits is {shares}. A buyer "
                     "that checks its own limits would not agree to those deals. Counting only deals within both "
                     "sides' limits, the order is "
                     + ", ".join(f"{k} {m[k]['joint_surplus_feasible_deals']:,.0f} TL" for k in feas) + ".")
    if b["no_deal_correctness"] is not None and b["no_deal_correctness"] < 1:
        lines.append(f"- BILNO's no-deal correctness is {_pct(b['no_deal_correctness'])}, not 100%: in some "
                     "scenarios it made a deal on its own simulation where the oracle, on the fresh draw, found none.")
    for k in MODELS:
        if m[k]["failed_limits"]:
            parts = ", ".join(f"`{name}` ×{n}" for name, n in m[k]["failed_limits"].items())
            lines.append(f"- {k}: limits its deals fail on the evaluation draw: {parts}.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--workers", type=int, default=None)
    args = ap.parse_args()
    t0 = time.time()
    scenarios = generate(args.n)
    with Pool(args.workers) as pool:
        results = []
        for i, r in enumerate(pool.imap(evaluate_scenario, scenarios), 1):
            results.append(r)
            if i % 20 == 0:
                print(f"  {i}/{len(scenarios)} scenarios ({time.time() - t0:.0f}s)", flush=True)
    m = metrics(results)
    integrity = execution_integrity()
    elapsed = time.time() - t0
    OUT_JSON.write_text(json.dumps({"scenario_seed": SCENARIO_SEED, "eval_seed": EVAL_SEED, "metrics": m,
                                    "execution_integrity": integrity,
                                    "scenarios": [asdict(s) for s in scenarios], "results": results},
                                   indent=1, default=str) + "\n")
    chart(m)
    write_doc(results, m, integrity, elapsed)
    print(json.dumps({k: v for k, v in m.items()}, indent=1, default=str))
    print(f"Execution integrity: {integrity['summary']}")
    print(f"Wrote {OUT_DOC.relative_to(REPO_ROOT)}, {OUT_PNG.relative_to(REPO_ROOT)}, {OUT_JSON.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
