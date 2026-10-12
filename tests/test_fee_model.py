"""Unit tests for the V8.1 verified Kalshi fee model.

Verifies:
- Exact CFTC quadratic taker fee formula: ceil(M * 0.07 * C * P * (1 - P))
- Maker fee coefficient (0.0175)
- Boundary price behavior (0¢, 1¢, 50¢, 99¢, 100¢)
- Zero fee at price extremes (0¢ and 100¢)
- Maximal fee rate at maximum uncertainty (50¢)
- Multi-tier fill handling and rounding modes (PER_ORDER_AGGREGATE vs PER_FILL_TIER vs PER_CONTRACT)
- Series-specific multipliers (e.g., KXINX 0.5, KXBTCY 0.0 exempt)
- Reversal of theoretical arbitrage profit into net loss under verified fees
- Fail-closed error handling on invalid configurations and parameters
"""

from decimal import Decimal
import pytest

from kalshi_arbitrage.fee_model import (
    CalculatedFee,
    FeeModelConfig,
    FeeModelError,
    FeeRoundingMode,
    FeeScheduleType,
    FillTier,
    calculate_multi_level_fee,
    calculate_single_level_fee,
    KALSHI_TAKER_RATE,
    KALSHI_MAKER_RATE,
)


def test_standard_taker_fee_at_50_cents():
    # 1 contract at 50 cents:
    # raw = 1.0 * 0.07 * 1 * (0.50 * 0.50) = 0.0175 dollars = 1.75 cents.
    # ceil(1.75 cents) = 2 cents.
    fee = calculate_single_level_fee(50, 1)
    assert fee.total_fee_cents == 2
    assert fee.total_fee_dollars == Decimal("0.02")
    assert fee.schedule_used == FeeScheduleType.KALSHI_OFFICIAL_TAKER
    assert fee.multiplier_used == Decimal("1.0")
    assert not fee.is_exempt


def test_aggregate_order_rounding_vs_per_contract():
    # 10 contracts at 50 cents:
    # Aggregate raw = 1.0 * 0.07 * 10 * 0.25 = 0.175 dollars = 17.5 cents.
    # PER_ORDER_AGGREGATE ceil(17.5) = 18 cents.
    fee_agg = calculate_single_level_fee(
        50, 10, config=FeeModelConfig(rounding_mode=FeeRoundingMode.PER_ORDER_AGGREGATE)
    )
    assert fee_agg.total_fee_cents == 18

    # PER_CONTRACT: ceil(1.75) = 2 cents per contract * 10 = 20 cents.
    fee_per_contract = calculate_single_level_fee(
        50, 10, config=FeeModelConfig(rounding_mode=FeeRoundingMode.PER_CONTRACT)
    )
    assert fee_per_contract.total_fee_cents == 20


def test_maker_fee_at_50_cents():
    # Maker rate is 0.0175 (25% of taker)
    # 10 contracts at 50 cents:
    # raw = 1.0 * 0.0175 * 10 * 0.25 = 0.04375 dollars = 4.375 cents.
    # ceil(4.375) = 5 cents.
    cfg = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_MAKER)
    fee = calculate_single_level_fee(50, 10, config=cfg)
    assert fee.total_fee_cents == 5
    assert fee.total_fee_dollars == Decimal("0.05")
    assert fee.schedule_used == FeeScheduleType.KALSHI_OFFICIAL_MAKER


def test_price_boundaries_zero_variance():
    # 0 cents: variance = 0 * 100 = 0 -> 0 cents fee
    fee_0 = calculate_single_level_fee(0, 100)
    assert fee_0.total_fee_cents == 0

    # 100 cents: variance = 100 * 0 = 0 -> 0 cents fee
    fee_100 = calculate_single_level_fee(100, 100)
    assert fee_100.total_fee_cents == 0


def test_low_price_boundary_1_cent():
    # 1 contract at 1 cent:
    # raw = 0.07 * 1 * (0.01 * 0.99) = 0.000693 dollars = 0.0693 cents.
    # ceil(0.0693) = 1 cent.
    fee_1 = calculate_single_level_fee(1, 1)
    assert fee_1.total_fee_cents == 1

    # 100 contracts at 1 cent:
    # raw = 0.07 * 100 * 0.0099 = 0.0693 dollars = 6.93 cents.
    # ceil(6.93) = 7 cents.
    fee_100 = calculate_single_level_fee(1, 100)
    assert fee_100.total_fee_cents == 7


def test_high_price_boundary_99_cents():
    # Symmetric with 1 cent due to p*(100-p) symmetry
    fee_99 = calculate_single_level_fee(99, 100)
    assert fee_99.total_fee_cents == 7


def test_series_multipliers_and_exemptions():
    # 1. Halved index series: KXINX (M = 0.5)
    # 10 contracts at 50 cents:
    # raw = 0.5 * 0.07 * 10 * 0.25 = 0.0875 dollars = 8.75 cents.
    # ceil(8.75) = 9 cents (compared to 18 cents standard).
    fee_inx = calculate_single_level_fee(50, 10, series_ticker="KXINX-24OCT")
    assert fee_inx.multiplier_used == Decimal("0.5")
    assert fee_inx.total_fee_cents == 9

    # 2. Fully exempt series: KXBTCY (M = 0.0)
    fee_btcy = calculate_single_level_fee(50, 100, series_ticker="KXBTCY-24DEC")
    assert fee_btcy.is_exempt is True
    assert fee_btcy.multiplier_used == Decimal("0.0")
    assert fee_btcy.total_fee_cents == 0


def test_multi_level_fill_fee_calculation():
    # Fill across 2 levels:
    # Tier 1: 5 contracts at 40¢
    #   p * (100 - p) / 100 = 40 * 60 / 100 = 24.0
    #   raw_1 = 0.07 * 5 * 24 / 100 = 0.084 cents * 100 = 8.4 cents.
    # Tier 2: 5 contracts at 60¢
    #   p * (100 - p) / 100 = 60 * 40 / 100 = 24.0
    #   raw_2 = 0.07 * 5 * 24 / 100 = 8.4 cents.
    # Aggregate raw = 8.4 + 8.4 = 16.8 cents.
    # ceil(16.8) = 17 cents.
    tiers = [FillTier(40, 5), FillTier(60, 5)]

    # Aggregate
    fee_agg = calculate_multi_level_fee(
        tiers, config=FeeModelConfig(rounding_mode=FeeRoundingMode.PER_ORDER_AGGREGATE)
    )
    assert fee_agg.total_fee_cents == 17

    # Per-tier:
    # Tier 1: ceil(8.4) = 9¢
    # Tier 2: ceil(8.4) = 9¢
    # Total = 18¢
    fee_per_tier = calculate_multi_level_fee(
        tiers, config=FeeModelConfig(rounding_mode=FeeRoundingMode.PER_FILL_TIER)
    )
    assert fee_per_tier.total_fee_cents == 18


def test_arbitrage_reversal_under_dynamic_fee():
    """Verify that an opportunity profitable under flat 1¢ fee turns into a loss under official fees."""
    # Scenario: Binary parity candidate at 49¢ YES and 49¢ NO.
    # Payout = 100¢.
    # Total acquisition cost = 49¢ + 49¢ = 98¢.
    # Gross edge = 100¢ - 98¢ = +2¢.

    # 1. Under legacy flat 1¢ per contract assumption:
    # Total contracts across 2 legs = 2.
    # Flat fee = 2 * 1¢ = 2¢.
    # Net edge = 2¢ - 2¢ = 0¢ (or positive if flat fee is 0.5¢ -> +1¢).
    flat_cfg = FeeModelConfig(schedule_type=FeeScheduleType.FLAT, flat_fee_cents=1)
    fee_yes_flat = calculate_single_level_fee(49, 1, config=flat_cfg).total_fee_cents
    fee_no_flat = calculate_single_level_fee(49, 1, config=flat_cfg).total_fee_cents
    assert fee_yes_flat + fee_no_flat == 2

    # 2. Under official Kalshi taker fee:
    # Leg 1 (49¢): raw = 0.07 * 1 * 0.49 * 0.51 = 0.017493 dollars = 1.7493 cents -> ceil = 2¢.
    # Leg 2 (49¢): raw = 0.07 * 1 * 0.49 * 0.51 = 1.7493 cents -> ceil = 2¢.
    # Total fee across both legs = 2¢ + 2¢ = 4¢!
    fee_yes_official = calculate_single_level_fee(49, 1).total_fee_cents
    fee_no_official = calculate_single_level_fee(49, 1).total_fee_cents
    total_official_fee = fee_yes_official + fee_no_official
    assert total_official_fee == 4

    gross_edge = 100 - (49 + 49)  # 2¢
    net_edge = gross_edge - total_official_fee  # 2¢ - 4¢ = -2¢ (NET LOSS!)
    assert net_edge == -2


def test_zero_quantity_handling():
    fee = calculate_single_level_fee(50, 0)
    assert fee.total_fee_cents == 0
    assert fee.total_contracts == 0

    fee_multi = calculate_multi_level_fee([])
    assert fee_multi.total_fee_cents == 0


def test_invalid_parameters_fail_closed():
    # Negative price
    with pytest.raises(FeeModelError):
        calculate_single_level_fee(-1, 10)

    # Price > 100
    with pytest.raises(FeeModelError):
        calculate_single_level_fee(101, 10)

    # Non-integer price
    with pytest.raises(FeeModelError):
        calculate_single_level_fee("50", 10)

    # Negative quantity
    with pytest.raises(FeeModelError):
        calculate_single_level_fee(50, -5)

    # Invalid FillTier quantity
    with pytest.raises(FeeModelError):
        FillTier(50, 0)


def test_centicent_precision_sub_cent_fee():
    """Verify that institutional direct members with CENTICENT precision are charged exact sub-cent fees."""
    from kalshi_arbitrage.fee_model import FeeTargetPrecision

    # 1 contract at 50¢:
    # Raw = 1.0 * 0.07 * 1 * (0.50 * 0.50) = 0.017500 dollars = 1.75 cents.
    # Non-direct member (CENT): ceil(1.75¢) = 2.00¢ ($0.02)
    fee_cent = calculate_single_level_fee(
        50, 1, config=FeeModelConfig(target_precision=FeeTargetPrecision.CENT)
    )
    assert fee_cent.total_fee_cents == 2
    assert fee_cent.total_fee_dollars == Decimal("0.02")
    assert fee_cent.fee_centicents == 200
    assert fee_cent.raw_fee_dollars == Decimal("0.0175")
    assert fee_cent.raw_fee_cents == Decimal("1.75")

    # Direct institutional member (CENTICENT): ceil up to $0.0001 (1 centicent) = $0.0175 (175 centicents)
    fee_centicent = calculate_single_level_fee(
        50, 1, config=FeeModelConfig(target_precision=FeeTargetPrecision.CENTICENT)
    )
    assert fee_centicent.total_fee_dollars == Decimal("0.0175")
    assert fee_centicent.fee_centicents == 175
    assert fee_centicent.raw_fee_dollars == Decimal("0.0175")
    assert fee_centicent.raw_fee_cents == Decimal("1.75")
    assert fee_centicent.total_fee_cents == 2  # Integer-cent projection for portfolios


def test_centicent_multi_tier_aggregation():
    """Verify multi-tier aggregation under centicent precision preserves sub-cent granularity."""
    from kalshi_arbitrage.fee_model import FeeTargetPrecision

    # Tier 1: 1 contract at 30¢: 1 * 0.07 * 0.30 * 0.70 = 0.0147 dollars (1.47 cents)
    # Tier 2: 1 contract at 70¢: 1 * 0.07 * 0.70 * 0.30 = 0.0147 dollars (1.47 cents)
    # Total raw = 0.0294 dollars = 2.94 cents.
    # CENT: ceil(2.94¢) = 3¢ ($0.03)
    tiers = [FillTier(30, 1), FillTier(70, 1)]
    fee_c = calculate_multi_level_fee(tiers, config=FeeModelConfig(target_precision=FeeTargetPrecision.CENT))
    assert fee_c.total_fee_cents == 3
    assert fee_c.total_fee_dollars == Decimal("0.03")

    # CENTICENT: round up to nearest $0.0001 = $0.0294 (294 centicents)
    fee_cc = calculate_multi_level_fee(tiers, config=FeeModelConfig(target_precision=FeeTargetPrecision.CENTICENT))
    assert fee_cc.total_fee_dollars == Decimal("0.0294")
    assert fee_cc.fee_centicents == 294
    assert fee_cc.raw_fee_dollars == Decimal("0.0294")
