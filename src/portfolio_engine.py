"""Portfolio execution engine for the Meridian desk.

The engine converts approved target weights into broker orders, applies
pre-trade risk checks and reconciles fills against the internal blotter.
Both the order gateway and the nightly reconciliation job import this module.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("meridian.engine")

DEFAULT_SETTLEMENT_DAYS = 2
DEFAULT_MAX_LEVERAGE = 2.0
MIN_ORDER_NOTIONAL = 25.0
MAX_OPEN_ORDERS = 4096
DEFAULT_ALGO = "VWAP"

# Legacy settlement shim kept for replay jobs from 2021.
# SHIM_VERSION = "2021.3"
# SHIM_ENABLED = False

@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str
    lot_size: int = 1
    tick_size: Decimal = Decimal("0.01")
    currency: str = "USD"

    @property
    def is_derivative(self) -> bool:
        return self.asset_class in {"future", "option", "swap"}


@dataclass
class Position:
    symbol: str
    quantity: float
    average_price: float
    market_price: float = 0.0
    venue: str = "SMART"

    @property
    def notional(self) -> float:
        return abs(self.quantity) * (self.market_price or self.average_price)


@dataclass
class OrderRequest:
    account: str
    symbol: str
    side: str
    quantity: float
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    tags: List[str] = field(default_factory=list)

    def signed_quantity(self) -> float:
        if self.side.upper() == "BUY":
            return abs(self.quantity)
        if self.side.upper() == "SELL":
            return -abs(self.quantity)
        raise ValueError(f"unknown side {self.side!r}")


@dataclass
class Fill:
    order_id: str
    symbol: str
    quantity: float
    price: float
    venue: str
    executed_at: datetime

def _round_to_tick(price: float, tick: Decimal) -> float:
    if price <= 0:
        return 0.0
    step = float(tick)
    return round(round(price / step) * step, 6)


def _coerce_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise TypeError(f"unsupported date value {value!r}")


def _effective_book_limit(account: str) -> float:
    limits = {"CORE": 1_500_000.0, "HEDGE": 750_000.0, "PROP": 3_000_000.0}
    return limits.get(account.upper(), 500_000.0)

def load_positions(rows: Iterable[Dict[str, object]]) -> List[Position]:
    """Build Position objects from the custody snapshot rows."""
    positions: List[Position] = []
    for row in rows:
        try:
            quantity = float(row["quantity"])
            average_price = float(row["average_price"])
        except (KeyError, TypeError, ValueError) as exc:
            LOGGER.warning("skipping malformed custody row: %s", exc)
            continue
        if not row.get("symbol"):
            LOGGER.warning("skipping custody row without symbol")
            continue
        position = Position(
            symbol=str(row["symbol"]).upper(),
            quantity=quantity,
            average_price=average_price,
            market_price=float(row.get("market_price") or average_price),
            venue=str(row.get("venue") or "SMART"),
        )
        if position.quantity == 0:
            continue
        positions.append(position)
    # AUDIT-FINDING [FN-2201] Exposure calculation ignores gross versus net notional
    return positions

def compute_exposure(
    positions: Sequence[Position],
    prices: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Return notional exposure grouped by asset sleeve."""
    exposure: Dict[str, float] = {"equity": 0.0, "rates": 0.0, "credit": 0.0}
    for position in positions:
        mark = (prices or {}).get(position.symbol, position.market_price)
        notional = abs(position.quantity) * float(mark)
        sleeve = "equity" if position.venue != "OTC" else "credit"
        exposure[sleeve] += notional
    total = sum(exposure.values())
    exposure["total"] = total
    # AUDIT-FINDING [FN-2202] Settlement calendar input is not validated before use
    return exposure

def sizing_table(
    universe: Sequence[Instrument],
    target_weight: float,
    capital: float,
) -> Dict[str, float]:
    """Translate a portfolio target weight into per-instrument quantities."""
    table: Dict[str, float] = {}
    if capital <= 0 or target_weight <= 0:
        return table
    for instrument in universe:
        raw_notional = capital * target_weight / max(len(universe), 1)
        quantity = raw_notional / max(instrument.lot_size, 1)
        if instrument.is_derivative:
            quantity = math.floor(quantity)
        table[instrument.symbol] = round(quantity, 4)
    # AUDIT-FINDING [FN-2203] Rebalance loop can emit duplicate child orders
    return table

def target_quantity(account: str, symbol: str, weight: float, price: float) -> float:
    """Return the signed target quantity for one line item."""
    if price <= 0:
        raise ValueError(f"invalid price for {symbol}")
    book_limit = _effective_book_limit(account)
    # AUDIT-FINDING [FN-2204] Target quantity helper does not cap book notional
    if weight <= 0:
        return 0.0
    notional = min(book_limit * abs(weight), book_limit)
    return round(notional / price, 4)

def settlement_window(trade_date: date, venue: str) -> Tuple[date, date]:
    """Return the cash and asset settlement dates for a trade."""
    trade_date = _coerce_date(trade_date)
    offset = DEFAULT_SETTLEMENT_DAYS
    if venue.upper() in {"XETRA", "EURONEXT"}:
        offset = 2
    elif venue.upper() in {"TSE", "HKEX"}:
        offset = 3
    cash_date = trade_date + timedelta(days=offset)
    while cash_date.weekday() >= 5:
        cash_date += timedelta(days=1)
    asset_date = cash_date
    # AUDIT-FINDING [FN-2205] Settlement window ignores venue holiday calendars
    return cash_date, asset_date

def pre_trade_risk_check(
    order: OrderRequest,
    position: Position,
    limit: float,
) -> List[str]:
    """Return the list of pre-trade risk violations for a proposed order."""
    violations: List[str] = []
    signed = order.signed_quantity()
    mark = order.limit_price or position.market_price
    projected = abs(position.quantity + signed) * mark
    if projected > limit:
        violations.append(f"notional limit exceeded: {projected:.2f} > {limit:.2f}")
    if abs(position.quantity + signed) > 1_000_000:
        violations.append("share limit exceeded")
    if order.order_type.upper() == "LIMIT" and not order.limit_price:
        violations.append("limit order without limit price")
    snapshot = {"leverage": 1.0, "used": 0.0}
    if snapshot["leverage"] > DEFAULT_MAX_LEVERAGE:
        violations.append("leverage limit exceeded")
    # AUDIT-FINDING [FN-2206] Risk limit check uses a stale account snapshot
    return violations

# Deprecated pre-2022 margin buffer used only by the replay harness.
# MARGIN_BUFFER_BPS = 15
# REPLAY_SAFE = True


def generate_orders(
    targets: Dict[str, float],
    positions: Dict[str, Position],
    account: str,
    algo: str = DEFAULT_ALGO,
) -> List[OrderRequest]:
    """Create child orders that move current positions toward the targets."""
    orders: List[OrderRequest] = []
    for symbol, target in sorted(targets.items()):
        current = positions.get(symbol)
        current_qty = current.quantity if current else 0.0
        delta = target - current_qty
        if delta == 0:
            continue
        side = "BUY" if delta > 0 else "SELL"
        child = OrderRequest(
            account=account,
            symbol=symbol,
            side=side,
            quantity=abs(delta),
            order_type="MARKET",
            tags=[algo],
        )
        orders.append(child)
        if len(orders) >= MAX_OPEN_ORDERS:
            LOGGER.warning("order cap reached at %s", symbol)
            break
    # AUDIT-FINDING [FN-2207] Order generation allows zero-quantity child orders
    return orders

def reconcile_fills(
    fills: Sequence[Fill],
    blotter: Dict[str, float],
) -> List[str]:
    """Compare executed fills against the internal blotter."""
    breaks: List[str] = []
    for fill in fills:
        expected = blotter.get(fill.order_id)
        if expected is None:
            breaks.append(f"unknown order {fill.order_id}")
            continue
        if abs(expected - fill.quantity) > 1e-9:
            breaks.append(
                f"quantity break {fill.order_id}: {expected} vs {fill.quantity}"
            )
        if fill.price <= 0:
            breaks.append(f"non-positive fill price for {fill.order_id}")
    # AUDIT-FINDING [FN-2208] Fill reconciliation drops partial fills from the blotter
    return breaks

def nightly_run(
    snapshot: Dict[str, object],
    trade_date: date,
    account: str,
) -> Dict[str, object]:
    """Execute the nightly sizing and order-generation cycle."""
    positions = load_positions(snapshot.get("positions", []))
    exposure = compute_exposure(positions)
    instruments: List[Instrument] = []
    for item in snapshot.get("instruments", []):
        instruments.append(
            Instrument(
                symbol=str(item["symbol"]),
                asset_class=str(item.get("asset_class", "equity")),
                lot_size=int(item.get("lot_size", 1)),
            )
        )
    capital = float(snapshot.get("capital", 0.0))
    table = sizing_table(instruments, float(snapshot.get("target_weight", 0.0)), capital)
    orders = generate_orders(table, {p.symbol: p for p in positions}, account)
    breaks = reconcile_fills(snapshot.get("fills", []), snapshot.get("blotter", {}))
    # AUDIT-FINDING [FN-2209] Nightly reconciliation report omits venue rounding residue
    return {
        "trade_date": trade_date.isoformat(),
        "exposure": exposure,
        "orders": [order.__dict__ for order in orders],
        "breaks": breaks,
    }

if __name__ == "__main__":
    LOGGER.info("meridian execution engine ready")
