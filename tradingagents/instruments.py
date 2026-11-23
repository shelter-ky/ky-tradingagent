"""Deterministic instrument profiles, independent of a successful quote lookup.

TQQQ: https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq
QQQ: https://www.invesco.com/qqq-etf/en/about.html
The registry describes product structure, never current holdings or prices.
"""
from dataclasses import asdict, dataclass, replace
import math
from numbers import Real

from tradingagents.dataflows.symbols import normalize_symbol

PROFILE_VERSION = "etf-v1"


@dataclass(frozen=True)
class InstrumentProfile:
    ticker: str
    kind: str = "stock"
    name: str = ""
    underlying_index: str | None = None
    research_proxy: str | None = None
    daily_leverage: float | None = None
    daily_reset: bool = False
    source: str = ""

    def to_dict(self):
        return asdict(self)


KNOWN_PROFILES = {
    "TQQQ": InstrumentProfile("TQQQ", "etf", "ProShares UltraPro QQQ",
                              "Nasdaq-100", "QQQ", 3.0, True,
                              "https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq"),
    "QQQ": InstrumentProfile("QQQ", "etf", "Invesco QQQ", "Nasdaq-100", "QQQ", 1.0, False,
                             "https://www.invesco.com/qqq-etf/en/about.html"),
}


def resolve_instrument_profile(ticker, asset_type="stock", identity=None, config=None):
    """Use explicit settings/known product structure before present-day metadata."""
    canonical = normalize_symbol(ticker)
    identity = identity or {}
    if canonical in KNOWN_PROFILES:
        profile = KNOWN_PROFILES[canonical]
    else:
        kind = "crypto" if asset_type == "crypto" else "etf" if (
            str(identity.get("quote_type", "")).upper() == "ETF" or asset_type == "etf"
        ) else "stock"
        profile = InstrumentProfile(canonical, kind, identity.get("company_name", ""))
    overrides = (config or {}).get("instrument_profiles", {}).get(canonical, {})
    if overrides:
        profile = replace(profile, **overrides)
    if profile.ticker != canonical or profile.kind not in {"stock", "crypto", "etf"}:
        raise ValueError("Instrument profile must preserve the ticker and use stock, crypto or etf")
    if profile.daily_leverage is not None and (
        isinstance(profile.daily_leverage, bool) or not isinstance(profile.daily_leverage, Real)
        or not math.isfinite(profile.daily_leverage)
        or profile.daily_leverage == 0
    ):
        raise ValueError("daily_leverage must be a finite nonzero number or None")
    if not isinstance(profile.daily_reset, bool):
        raise ValueError("daily_reset must be a boolean")
    return profile


def profile_from_state(state):
    from tradingagents.dataflows.config import get_config
    saved = state.get("instrument_profile")
    if isinstance(saved, dict) and saved:
        canonical = normalize_symbol(str(state["company_of_interest"]))
        return resolve_instrument_profile(canonical, state.get("asset_type", "stock"),
                                          config={"instrument_profiles": {canonical: saved}})
    return resolve_instrument_profile(str(state["company_of_interest"]), state.get("asset_type", "stock"),
                                      config=get_config())
