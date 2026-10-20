"""UI smoke test: every page renders without an exception, even when the services are not running."""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

UI = Path(__file__).resolve().parents[1] / "ui"
PAGES = [UI / "Home.py", *sorted((UI / "pages").glob("*.py"))]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_page_renders_without_services(page, monkeypatch):
    for var, port in (("SUPPLIER_URL", 1), ("BUYER_URL", 2), ("COORDINATOR_URL", 3), ("BANK_URL", 4)):
        monkeypatch.setenv(var, f"http://127.0.0.1:{port}")  # nothing listens there
    st.cache_resource.clear()  # the UI caches its HTTP clients (and their URLs) per process
    at = AppTest.from_file(str(page), default_timeout=30).run()
    assert not at.exception, at.exception


@pytest.fixture(scope="module")
def live_services(tmp_path_factory):
    """The four real services (ledger disabled, offline agents) on free ports."""
    from services.coordinator.run_flow import SERVICES, start_stack, stop_stack
    from services.ledger import local_chain

    ports = {n: local_chain.free_port() for n in SERVICES}
    procs, url, _ = start_stack("none", tmp_path_factory.mktemp("ui-live"), ports, offline=True)
    yield url
    stop_stack(procs)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_page_renders_with_live_services(page, live_services, monkeypatch):
    """With data flowing, charts (incl. the Stress Lab's before/after chart) must build and validate."""
    for var, name in (("SUPPLIER_URL", "supplier"), ("BUYER_URL", "buyer"), ("COORDINATOR_URL", "coordinator"),
                      ("BANK_URL", "bank")):
        monkeypatch.setenv(var, live_services[name])
    st.cache_resource.clear()
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception, at.exception
    # st.error is also used for the intended crisis alert; fail only on unreachable services or failed API calls
    failures = [e.value for e in at.error if "not reachable" in e.value or " -> " in e.value]
    assert not failures, failures
