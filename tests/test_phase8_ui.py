"""UI smoke test: every page renders without an exception, even when the services are not running."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

UI = Path(__file__).resolve().parents[1] / "ui"
PAGES = [UI / "Home.py", *sorted((UI / "pages").glob("*.py"))]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_page_renders_without_services(page, monkeypatch):
    for var, port in (("SUPPLIER_URL", 1), ("BUYER_URL", 2), ("COORDINATOR_URL", 3), ("BANK_URL", 4)):
        monkeypatch.setenv(var, f"http://127.0.0.1:{port}")  # nothing listens there
    at = AppTest.from_file(str(page), default_timeout=30).run()
    assert not at.exception, at.exception
