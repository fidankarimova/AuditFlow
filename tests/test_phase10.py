"""Experiments: deterministic scenario generation and one full evaluation per regime."""

from experiments import run


def test_scenarios_are_reproducible_and_cover_all_regimes():
    a, b = run.generate(40), run.generate(40)
    assert a == b
    assert {s.regime for s in a} == {"crisis", "no_need", "tight_buyer", "costly_buyer"}


def test_evaluation_uses_production_policies_and_a_fresh_draw():
    scenarios = {s.regime: s for s in run.generate(40)}
    for regime, sc in scenarios.items():
        r = run.evaluate_scenario(sc)
        assert set(r["models"]) == set(run.MODELS)
        if regime == "no_need":
            assert not r["crisis"] and r["models"]["BILNO"]["deal"] is None
        if r["models"]["BILNO"]["deal"]:  # BILNO never makes a deal the buyer loses money on
            assert r["models"]["BILNO"]["buyer_gain"] > 0
    assert run.EVAL_SEED != 42  # models use the engine default seed; evaluation uses another
