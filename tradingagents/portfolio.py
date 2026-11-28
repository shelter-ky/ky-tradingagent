# Modified for KY TradingAgent 0.0.2 (2026-11-28).
"""The caller's book, as the decision agents see it.

Optional input to a run: what is held, at what average price, and how much cash
is free. Without it the agents cannot tell adding to a full position from
opening a new one. Three states are distinct and must stay so: a position, a
flat book, and no context at all, since treating "not provided" as "flat" would
invent a fact about the caller's account.

Broker-neutral by construction: quantities are generic units and the currency is
whatever label the caller passes, so nothing here implies a venue or an
execution path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from tradingagents.dataflows.symbols import normalize_symbol


class Position(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    ticker: str = Field(description="Instrument symbol, e.g. AAPL")
    quantity: float = Field(description="Signed units held; negative is short")
    available_quantity: float | None = Field(default=None, ge=0, strict=True)
    average_price: float | None = Field(default=None, description="Average entry price per unit")

    @field_validator("quantity", "average_price", mode="before")
    @classmethod
    def _no_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("A quantity/price cannot be a boolean")
        return value


class ReferenceQuote(BaseModel):
    """Caller-recorded, unadjusted per-share reference; never a model price."""
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    ticker: str
    price: float = Field(gt=0)
    currency: str
    as_of_date: date
    price_basis: str = Field(description="Must explicitly be 'unadjusted' for sizing")

    @field_validator("price", mode="before")
    @classmethod
    def _no_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("A price cannot be a boolean")
        return value


class PortfolioContext(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    cash: float | None = Field(default=None, description="Free cash available")
    currency: str | None = Field(default=None, description="Currency label for cash and prices")
    positions: list[Position] = Field(default_factory=list)
    net_asset_value: float | None = Field(default=None, gt=0, description="Caller-reported net liquidation value, not cost basis")
    as_of_date: date | None = None
    as_of_time: AwareDatetime | None = None
    intraday_stop_prices: dict[str, Annotated[float, Field(gt=0, strict=True)]] = Field(default_factory=dict)
    reference_quotes: list[ReferenceQuote] = Field(default_factory=list)

    @field_validator("cash", "net_asset_value", mode="before")
    @classmethod
    def _no_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("An account value cannot be a boolean")
        return value

    def positions_in(self, ticker: str) -> list[Position]:
        """The holdings of the instrument the run prices for ``ticker``, under any
        broker spelling (BTCUSDT, 700.HK). Holdings keep the spelling they were
        given: a symbol can also name another instrument (WTI the stock)."""
        symbol = normalize_symbol(ticker)
        return [p for p in self.positions if normalize_symbol(p.ticker) == symbol]

    def position_in(self, ticker: str) -> Position | None:
        held = self.positions_in(ticker)
        return held[0] if held else None

    def render(self, ticker: str) -> str:
        """The portfolio block for the decision agents, led by the analyzed instrument."""
        symbol = normalize_symbol(ticker)
        held = self.positions_in(symbol)
        lines = []
        for position in held:
            price = f", average price {position.average_price:,.2f}" if position.average_price is not None else ""
            spelled = position.ticker.strip().upper()
            listed = f" (listed as {position.ticker})" if spelled != symbol else ""
            lines.append(f"- Current position in {symbol}: {position.quantity:,.4g} units{price}{listed}")
        if not held:
            lines.append(f"- No current position in {symbol}")
        if self.cash is not None:
            lines.append(f"- Cash available: {self.cash:,.2f}{' ' + self.currency if self.currency else ''}")
        others = [p for p in self.positions if all(p is not h for h in held)]
        if others:
            lines.append("- Other positions: " + ", ".join(f"{p.ticker.upper()} {p.quantity:,.4g}" for p in others))
        if self.net_asset_value is not None:
            lines.append(f"- Caller-reported net asset value: {self.net_asset_value:,.2f} {self.currency or '(currency missing)'}")
        if self.as_of_date is not None:
            lines.append(f"- Portfolio snapshot date: {self.as_of_date}")
        for quote in self.reference_quotes:
            if normalize_symbol(quote.ticker) == symbol:
                lines.append(f"- Caller reference quote: {quote.ticker} {quote.price:g} {quote.currency}, "
                             f"dated {quote.as_of_date}, basis {quote.price_basis}; not independently market-verified")
        return "Portfolio at the analysis date:\n" + "\n".join(lines)

    def fingerprint(self) -> str:
        """Stable digest of the book, so a changed one cannot resume a stale run."""
        # Preserve the serialized shape of date-only books from older checkpoints.
        exclude = {}
        if self.as_of_time is None:
            exclude["as_of_time"] = True
        if not self.intraday_stop_prices:
            exclude["intraday_stop_prices"] = True
        absent = {i: {"available_quantity"} for i, p in enumerate(self.positions) if p.available_quantity is None}
        if absent:
            exclude["positions"] = absent
        return hashlib.sha256(self.model_dump_json(exclude=exclude).encode()).hexdigest()[:12]


def load_portfolio(path: str | Path) -> PortfolioContext:
    """Read a portfolio JSON file, failing before the run rather than mid-graph."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return PortfolioContext.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"portfolio file {path} is not usable: {exc}") from exc
