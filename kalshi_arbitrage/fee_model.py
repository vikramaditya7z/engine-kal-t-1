"""Verified Kalshi fee schedules and deterministic fee calculation engine.

V8.1 Fee Modeling:
Replaces simplistic flat-fee assumptions with the official, CFTC-regulated
quadratic fee schedule used by Kalshi for event contracts.

Authoritative Exchange Rules & Sources:
- Kalshi Regulatory Fee Schedule (CFTC DCM Rules): https://kalshi.com/regulatory/fee-schedule
- Kalshi Fee Schedule Overview: https://kalshi.com/fee-schedule
- Kalshi Developer API Fee Rounding Documentation: https://kalshi-b198743e.mintlify.app/fee-rounding
- Date Verified: October 2026.

Governing Formula:
    Fee = round_up(M * Rate * C * P * (1 - P))  [in dollars]
    In exact integer cents:
    fee_raw_cents = M * Rate * C * (p * (100 - p) / 100)
    fee_cents = ceil(fee_raw_cents)

Where:
- P: Contract price in dollars in [0.00, 1.00] (or p in integer cents in [0, 100]).
- C: Number of contracts traded (positive integer).
- Rate: Exchange fee rate coefficient.
  - Taker: 0.0700 (7.0%)
  - Maker: 0.0175 (1.75%, or 25% of taker rate)
- M: Contract series multiplier (default 1.0; 0.5 for select index series; 0.0 for exempt series).
- round_up: Rounded up to the nearest whole cent (or centicent $0.0001 for sub-cent accounts).
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
from enum import Enum
import math
from typing import Mapping, Optional, Sequence, Tuple, Union

from .contract import ContractInputError


class FeeScheduleType(str, Enum):
    """Supported fee schedule models."""

    KALSHI_OFFICIAL_TAKER = "KALSHI_OFFICIAL_TAKER"
    KALSHI_OFFICIAL_MAKER = "KALSHI_OFFICIAL_MAKER"
    FLAT = "FLAT"
    EXEMPT = "EXEMPT"


class FeeTargetPrecision(str, Enum):
    """Target accounting precision for trade fee rounding."""

    CENT = "CENT"              # Non-direct members: rounded up to nearest whole cent ($0.01)
    CENTICENT = "CENTICENT"    # Direct members: rounded up to nearest centicent ($0.0001 = 0.01¢)


class FeeRoundingMode(str, Enum):
    """Granularity for rounding fee calculations."""

    PER_ORDER_AGGREGATE = "PER_ORDER_AGGREGATE"  # Official default: ceil on aggregate order fill
    PER_FILL_TIER = "PER_FILL_TIER"              # Ceil on each consumed book level independently
    PER_CONTRACT = "PER_CONTRACT"                # Conservative: ceil per single contract * quantity


# Default rates from official Kalshi CFTC filings
KALSHI_TAKER_RATE = Decimal("0.0700")
KALSHI_MAKER_RATE = Decimal("0.0175")
DEFAULT_MULTIPLIER = Decimal("1.0")

# Known official series exceptions
KNOWN_SERIES_MULTIPLIERS: Mapping[str, Decimal] = {
    "KXBTCY": Decimal("0.0"),      # Crypto annual zero-fee promotion
    "KXCITRINI": Decimal("0.0"),   # Co-branded zero-fee promotion
    "KXINX": Decimal("0.5"),       # S&P 500 index contracts (halved fee)
    "KXNASDAQ100": Decimal("0.5"), # Nasdaq index contracts (halved fee)
}


class FeeModelError(ValueError):
    """Raised when fee calculation encounters invalid, malformed, or ambiguous parameters."""


@dataclass(frozen=True)
class FeeModelConfig:
    """Configuration for deterministic fee calculations."""

    schedule_type: FeeScheduleType = FeeScheduleType.KALSHI_OFFICIAL_TAKER
    rate: Decimal = KALSHI_TAKER_RATE
    default_multiplier: Decimal = DEFAULT_MULTIPLIER
    series_multipliers: Mapping[str, Decimal] = field(default_factory=lambda: dict(KNOWN_SERIES_MULTIPLIERS))
    rounding_mode: FeeRoundingMode = FeeRoundingMode.PER_ORDER_AGGREGATE
    flat_fee_cents: int = 1  # Used only when schedule_type == FLAT
    target_precision: FeeTargetPrecision = FeeTargetPrecision.CENT

    def __post_init__(self) -> None:
        if not isinstance(self.schedule_type, FeeScheduleType):
            raise FeeModelError(f"invalid schedule_type: {self.schedule_type!r}")
        if not isinstance(self.target_precision, FeeTargetPrecision):
            raise FeeModelError(f"invalid target_precision: {self.target_precision!r}")
        if self.rate < Decimal("0"):
            raise FeeModelError("fee rate cannot be negative")
        if self.default_multiplier < Decimal("0"):
            raise FeeModelError("default_multiplier cannot be negative")
        if self.flat_fee_cents < 0:
            raise FeeModelError("flat_fee_cents cannot be negative")

    def get_multiplier_for_series(self, series_ticker: Optional[str]) -> Decimal:
        """Resolve contract multiplier for a specific market series ticker."""
        if not series_ticker:
            return self.default_multiplier
        clean_prefix = series_ticker.strip().upper()
        # Direct lookup or prefix match
        if clean_prefix in self.series_multipliers:
            return self.series_multipliers[clean_prefix]
        for prefix, mult in self.series_multipliers.items():
            if clean_prefix.startswith(prefix):
                return mult
        return self.default_multiplier


@dataclass(frozen=True)
class FillTier:
    """A price level and contract quantity consumed in a fill."""

    price_cents: int
    quantity: int

    def __post_init__(self) -> None:
        if isinstance(self.price_cents, bool) or not isinstance(self.price_cents, int):
            raise FeeModelError("price_cents must be an integer")
        if not 0 <= self.price_cents <= 100:
            raise FeeModelError(f"price_cents must be between 0 and 100, got {self.price_cents}")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise FeeModelError("quantity must be an integer")
        if self.quantity <= 0:
            raise FeeModelError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True)
class CalculatedFee:
    """Detailed result of a deterministic fee calculation."""

    total_fee_cents: int
    total_fee_dollars: Decimal
    effective_rate_bps: Optional[Decimal]
    schedule_used: FeeScheduleType
    rounding_mode_used: FeeRoundingMode
    multiplier_used: Decimal
    total_contracts: int
    is_exempt: bool
    explanation: str
    fee_centicents: int = 0
    raw_fee_dollars: Decimal = Decimal("0.000000")
    raw_fee_cents: Decimal = Decimal("0.0000")
    target_precision: FeeTargetPrecision = FeeTargetPrecision.CENT


def calculate_single_level_fee(
    price_cents: int,
    quantity: int,
    *,
    config: Optional[FeeModelConfig] = None,
    series_ticker: Optional[str] = None,
) -> CalculatedFee:
    """Calculate the fee for a single price level fill in exact integer cents or centicents."""
    cfg = config or FeeModelConfig()
    if isinstance(price_cents, bool) or not isinstance(price_cents, int):
        raise FeeModelError("price_cents must be an integer")
    if not 0 <= price_cents <= 100:
        raise FeeModelError(f"price_cents must be between 0 and 100, got {price_cents}")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise FeeModelError("quantity must be an integer")
    if quantity < 0:
        raise FeeModelError(f"quantity cannot be negative, got {quantity}")

    if quantity == 0:
        return CalculatedFee(
            total_fee_cents=0,
            total_fee_dollars=Decimal("0.00"),
            effective_rate_bps=Decimal("0"),
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=Decimal("1.0"),
            total_contracts=0,
            is_exempt=False,
            explanation="Zero quantity traded; fee is 0",
            fee_centicents=0,
            raw_fee_dollars=Decimal("0.000000"),
            raw_fee_cents=Decimal("0.0000"),
            target_precision=cfg.target_precision,
        )

    # 1. FLAT fee schedule
    if cfg.schedule_type == FeeScheduleType.FLAT:
        fee_cents = quantity * cfg.flat_fee_cents
        fee_dollars = (Decimal(fee_cents) / Decimal("100")).quantize(Decimal("0.01"))
        return CalculatedFee(
            total_fee_cents=fee_cents,
            total_fee_dollars=fee_dollars,
            effective_rate_bps=None,
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=Decimal("1.0"),
            total_contracts=quantity,
            is_exempt=False,
            explanation=f"Flat fee: {cfg.flat_fee_cents}¢ x {quantity} contracts = {fee_cents}¢",
            fee_centicents=fee_cents * 100,
            raw_fee_dollars=fee_dollars,
            raw_fee_cents=Decimal(fee_cents),
            target_precision=cfg.target_precision,
        )

    # 2. EXEMPT schedule
    if cfg.schedule_type == FeeScheduleType.EXEMPT:
        return CalculatedFee(
            total_fee_cents=0,
            total_fee_dollars=Decimal("0.00"),
            effective_rate_bps=Decimal("0"),
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=Decimal("0.0"),
            total_contracts=quantity,
            is_exempt=True,
            explanation="Market series is exempt from trading fees",
            fee_centicents=0,
            raw_fee_dollars=Decimal("0.000000"),
            raw_fee_cents=Decimal("0.0000"),
            target_precision=cfg.target_precision,
        )

    # 3. Official Quadratic Model (TAKER or MAKER)
    mult = cfg.get_multiplier_for_series(series_ticker)
    if mult == Decimal("0"):
        return CalculatedFee(
            total_fee_cents=0,
            total_fee_dollars=Decimal("0.00"),
            effective_rate_bps=Decimal("0"),
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=mult,
            total_contracts=quantity,
            is_exempt=True,
            explanation=f"Series {series_ticker or 'UNKNOWN'} multiplier is 0.0 (fee exempt)",
            fee_centicents=0,
            raw_fee_dollars=Decimal("0.000000"),
            raw_fee_cents=Decimal("0.0000"),
            target_precision=cfg.target_precision,
        )

    rate = cfg.rate
    if cfg.schedule_type == FeeScheduleType.KALSHI_OFFICIAL_MAKER:
        rate = KALSHI_MAKER_RATE if cfg.rate == KALSHI_TAKER_RATE else cfg.rate

    # If price is at boundaries (0¢ or 100¢), p*(100-p) = 0
    if price_cents == 0 or price_cents == 100:
        return CalculatedFee(
            total_fee_cents=0,
            total_fee_dollars=Decimal("0.00"),
            effective_rate_bps=Decimal("0"),
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=mult,
            total_contracts=quantity,
            is_exempt=False,
            explanation=f"Boundary price {price_cents}¢ has zero variance; fee is 0¢",
            fee_centicents=0,
            raw_fee_dollars=Decimal("0.000000"),
            raw_fee_cents=Decimal("0.0000"),
            target_precision=cfg.target_precision,
        )

    p_dec = Decimal(price_cents) / Decimal("100")
    # Official CFTC formula: M * Rate * C * P * (1 - P) [in dollars]
    raw_dollars = mult * rate * Decimal(quantity) * p_dec * (Decimal("1") - p_dec)
    raw_cents = raw_dollars * Decimal("100")

    if cfg.rounding_mode == FeeRoundingMode.PER_CONTRACT:
        single_raw_dollars = mult * rate * p_dec * (Decimal("1") - p_dec)
        if cfg.target_precision == FeeTargetPrecision.CENTICENT:
            single_dollars = single_raw_dollars.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
            fee_dollars = single_dollars * Decimal(quantity)
            fee_centicents = int((fee_dollars * Decimal("10000")).to_integral_value())
            fee_cents = int((fee_dollars * Decimal("100")).to_integral_value(rounding=ROUND_CEILING))
        else:
            single_raw_cents = single_raw_dollars * Decimal("100")
            single_fee_cents = int(single_raw_cents.to_integral_value(rounding=ROUND_CEILING))
            fee_cents = single_fee_cents * quantity
            fee_dollars = (Decimal(fee_cents) / Decimal("100")).quantize(Decimal("0.01"))
            fee_centicents = fee_cents * 100
        raw_fee_dollars = raw_dollars
        raw_fee_cents = raw_cents
    else:
        # PER_ORDER_AGGREGATE or PER_FILL_TIER
        raw_fee_dollars = raw_dollars
        raw_fee_cents = raw_cents
        if cfg.target_precision == FeeTargetPrecision.CENTICENT:
            fee_dollars = raw_fee_dollars.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
            fee_centicents = int((fee_dollars * Decimal("10000")).to_integral_value())
            fee_cents = int((fee_dollars * Decimal("100")).to_integral_value(rounding=ROUND_CEILING))
        else:
            fee_cents = int(raw_fee_cents.to_integral_value(rounding=ROUND_CEILING))
            fee_dollars = (Decimal(fee_cents) / Decimal("100")).quantize(Decimal("0.01"))
            fee_centicents = fee_cents * 100

    total_notional_cents = price_cents * quantity
    bps = (
        (Decimal(fee_cents) * Decimal("10000") / Decimal(total_notional_cents)).quantize(Decimal("0.1"))
        if total_notional_cents > 0
        else None
    )

    return CalculatedFee(
        total_fee_cents=fee_cents,
        total_fee_dollars=fee_dollars,
        effective_rate_bps=bps,
        schedule_used=cfg.schedule_type,
        rounding_mode_used=cfg.rounding_mode,
        multiplier_used=mult,
        total_contracts=quantity,
        is_exempt=False,
        explanation=(
            f"Official {cfg.schedule_type.value}: ceil(M({mult}) x Rate({rate}) x "
            f"Qty({quantity}) x {price_cents}¢ x {100-price_cents}¢ / 100) = {fee_dollars}$ ({fee_centicents} centicents, {fee_cents}¢)"
        ),
        fee_centicents=fee_centicents,
        raw_fee_dollars=raw_fee_dollars,
        raw_fee_cents=raw_fee_cents,
        target_precision=cfg.target_precision,
    )


def calculate_multi_level_fee(
    fill_tiers: Sequence[FillTier],
    *,
    config: Optional[FeeModelConfig] = None,
    series_ticker: Optional[str] = None,
) -> CalculatedFee:
    """Calculate the fee across multiple consumed order-book price tiers."""
    cfg = config or FeeModelConfig()
    if not fill_tiers:
        return CalculatedFee(
            total_fee_cents=0,
            total_fee_dollars=Decimal("0.00"),
            effective_rate_bps=Decimal("0"),
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=Decimal("1.0"),
            total_contracts=0,
            is_exempt=False,
            explanation="Empty fill tiers; fee is 0",
            fee_centicents=0,
            raw_fee_dollars=Decimal("0.000000"),
            raw_fee_cents=Decimal("0.0000"),
            target_precision=cfg.target_precision,
        )

    total_qty = sum(t.quantity for t in fill_tiers)
    if total_qty == 0:
        return calculate_single_level_fee(0, 0, config=cfg, series_ticker=series_ticker)

    # FLAT or EXEMPT
    if cfg.schedule_type in (FeeScheduleType.FLAT, FeeScheduleType.EXEMPT):
        return calculate_single_level_fee(50, total_qty, config=cfg, series_ticker=series_ticker)

    mult = cfg.get_multiplier_for_series(series_ticker)
    if mult == Decimal("0"):
        return CalculatedFee(
            total_fee_cents=0,
            total_fee_dollars=Decimal("0.00"),
            effective_rate_bps=Decimal("0"),
            schedule_used=cfg.schedule_type,
            rounding_mode_used=cfg.rounding_mode,
            multiplier_used=mult,
            total_contracts=total_qty,
            is_exempt=True,
            explanation=f"Series {series_ticker or 'UNKNOWN'} multiplier is 0.0 (fee exempt)",
            fee_centicents=0,
            raw_fee_dollars=Decimal("0.000000"),
            raw_fee_cents=Decimal("0.0000"),
            target_precision=cfg.target_precision,
        )

    rate = cfg.rate
    if cfg.schedule_type == FeeScheduleType.KALSHI_OFFICIAL_MAKER:
        rate = KALSHI_MAKER_RATE if cfg.rate == KALSHI_TAKER_RATE else cfg.rate

    if cfg.rounding_mode in (FeeRoundingMode.PER_FILL_TIER, FeeRoundingMode.PER_CONTRACT):
        tier_fees = [
            calculate_single_level_fee(t.price_cents, t.quantity, config=cfg, series_ticker=series_ticker)
            for t in fill_tiers
        ]
        total_fee_cents = sum(f.total_fee_cents for f in tier_fees)
        total_fee_dollars = sum(f.total_fee_dollars for f in tier_fees)
        fee_centicents = sum(f.fee_centicents for f in tier_fees)
        raw_fee_dollars = sum(f.raw_fee_dollars for f in tier_fees)
        raw_fee_cents = sum(f.raw_fee_cents for f in tier_fees)
    else:
        # Official PER_ORDER_AGGREGATE: sum raw continuous dollars across tiers, then round to target precision
        raw_dollars_sum = Decimal("0")
        for t in fill_tiers:
            p_dec = Decimal(t.price_cents) / Decimal("100")
            raw_dollars_sum += mult * rate * Decimal(t.quantity) * p_dec * (Decimal("1") - p_dec)
        raw_fee_dollars = raw_dollars_sum
        raw_fee_cents = raw_dollars_sum * Decimal("100")
        if cfg.target_precision == FeeTargetPrecision.CENTICENT:
            total_fee_dollars = raw_fee_dollars.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
            fee_centicents = int((total_fee_dollars * Decimal("10000")).to_integral_value())
            total_fee_cents = int((total_fee_dollars * Decimal("100")).to_integral_value(rounding=ROUND_CEILING))
        else:
            total_fee_cents = int(raw_fee_cents.to_integral_value(rounding=ROUND_CEILING))
            total_fee_dollars = (Decimal(total_fee_cents) / Decimal("100")).quantize(Decimal("0.01"))
            fee_centicents = total_fee_cents * 100

    total_notional_cents = sum(t.price_cents * t.quantity for t in fill_tiers)
    bps = (
        (Decimal(total_fee_cents) * Decimal("10000") / Decimal(total_notional_cents)).quantize(Decimal("0.1"))
        if total_notional_cents > 0
        else None
    )

    return CalculatedFee(
        total_fee_cents=total_fee_cents,
        total_fee_dollars=total_fee_dollars,
        effective_rate_bps=bps,
        schedule_used=cfg.schedule_type,
        rounding_mode_used=cfg.rounding_mode,
        multiplier_used=mult,
        total_contracts=total_qty,
        is_exempt=False,
        explanation=(
            f"Multi-tier fill ({len(fill_tiers)} tiers, {total_qty} contracts) under "
            f"{cfg.schedule_type.value} ({cfg.rounding_mode.value}) = {total_fee_dollars}$ ({fee_centicents} centicents, {total_fee_cents}¢)"
        ),
        fee_centicents=fee_centicents,
        raw_fee_dollars=raw_fee_dollars,
        raw_fee_cents=raw_fee_cents,
        target_precision=cfg.target_precision,
    )
