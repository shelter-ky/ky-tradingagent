"""Public current-vintage CSV is useful live, never a replacement for ALFRED."""
from types import SimpleNamespace

import pytest
import requests

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import fred


@pytest.fixture
def public(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(fred, "_fred_today", lambda: "2026-11-09")
    def download(url, **kwargs):
        assert url == fred.FRED_PUBLIC_CSV
        assert kwargs["params"] == {"id": "DGS10"}
        return SimpleNamespace(text="observation_date,DGS10\n2026-11-05,4.8\n2026-11-06,.\n2026-11-07,inf\n2026-11-10,99\n")
    monkeypatch.setattr(fred, "get_scrubbed", download)


def test_missing_key_live_uses_official_csv(public):
    out = fred.get_macro_data("10y_treasury", "2026-11-09", 10)
    assert "**Latest:** 4.8 (2026-11-05)" in out
    assert "current revised values" in out
    # The real fetch timestamp may be Nov 10 or later; only observation rows
    # must be bounded by the simulated FRED date.
    assert "| 2026-11-10 |" not in out and "| inf |" not in out


def test_historical_missing_key_never_downloads_current_csv(public, monkeypatch):
    monkeypatch.setattr(fred, "get_scrubbed", lambda *a, **k: pytest.fail("historical request leaked to current CSV"))
    with pytest.raises(fred.FredNotConfiguredError):
        fred.get_macro_data("10y_treasury", "2026-11-05", 10)


def test_local_date_ahead_of_fred_is_clamped(public):
    out = fred.get_macro_data("10y_treasury", "2026-11-10", 10)
    assert "Window: 2026-10-31 to 2026-11-09" in out
    assert "| 2026-11-10 |" not in out


def test_public_timeout_is_typed_failure(public, monkeypatch):
    def timeout(*a, **k):
        raise requests.Timeout("timeout")
    monkeypatch.setattr(fred, "get_scrubbed", timeout)
    with pytest.raises(VendorUnavailableError, match="Timeout"):
        fred.get_macro_data("10y_treasury", "2026-11-09")


def test_html_response_is_not_macro_data(public, monkeypatch):
    monkeypatch.setattr(fred, "get_scrubbed", lambda *a, **k: SimpleNamespace(text="<html>unavailable</html>"))
    with pytest.raises(VendorUnavailableError, match="requested CSV"):
        fred.get_macro_data("10y_treasury", "2026-11-09")
