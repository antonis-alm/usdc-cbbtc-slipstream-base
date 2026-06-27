from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from almanak.framework.market import PriceUnavailableError

from strategy import UsdcCbbtcSlipstreamBaseStrategy


class _Envelope:
    def __init__(self, value):
        self.value = value


class FakeMarket:
    def __init__(
        self,
        *,
        usdc_price: Decimal = Decimal("1"),
        cbbtc_price: Decimal = Decimal("60000"),
        fee_apy: Decimal = Decimal("60"),
        token0_weight: Decimal = Decimal("0.5"),
        atr_value_percent: Decimal = Decimal("1.0"),
        low_24h: Decimal = Decimal("58000"),
        high_24h: Decimal = Decimal("62000"),
        il_percent: Decimal = Decimal("1.0"),
        usdc_balance: Decimal = Decimal("20000"),
        cbbtc_balance: Decimal = Decimal("0.4"),
        usdc_balance_usd: Decimal = Decimal("20000"),
        cbbtc_balance_usd: Decimal = Decimal("24000"),
    ) -> None:
        self._usdc_price = usdc_price
        self._cbbtc_price = cbbtc_price
        self._pool_analytics = _Envelope(
            SimpleNamespace(
                fee_apy=fee_apy,
                token0_weight=token0_weight,
            )
        )
        self._atr = SimpleNamespace(value_percent=atr_value_percent)
        self._price_data = SimpleNamespace(low_24h=low_24h, high_24h=high_24h)
        self._projected_il = SimpleNamespace(il_percent=il_percent)
        self._balances = {
            "USDC": SimpleNamespace(balance=usdc_balance, balance_usd=usdc_balance_usd),
            "CBBTC": SimpleNamespace(balance=cbbtc_balance, balance_usd=cbbtc_balance_usd),
        }

    def price(self, token: str):
        if token == "USDC":
            return self._usdc_price
        if token == "CBBTC":
            return self._cbbtc_price
        raise ValueError(f"unknown token {token}")

    def pool_analytics(self, *args, **kwargs):
        return self._pool_analytics

    def atr(self, *args, **kwargs):
        return self._atr

    def price_data(self, token: str):
        if token != "CBBTC":
            raise ValueError("unsupported token")
        return self._price_data

    def projected_il(self, *args, **kwargs):
        return self._projected_il

    def balance(self, token: str, **kwargs):
        return self._balances[token]


@pytest.fixture
def base_config() -> dict:
    return {
        "chain": "base",
        "protocol": "aerodrome_slipstream",
        "pool_name": "USDC-CBBTC",
        "pool": "USDC/CBBTC/100",
        "pool_address": "0x4e962bb3889bf030368f56810a9c96b83cb3e778",
        "force_action": "",
        "force_position_id": None,
        "max_capital_deploy_pct": "80",
        "capital_reserve_pct": 20,
        "initial_tranche_pct": 50,
        "followup_tranche_pct": 30,
        "min_deploy_tranche_usd": 25,
        "entry_require_price_within_recent_band_pct": 1.2,
        "entry_max_volatility_1h_pct": 2,
        "cooldown_after_rebalance_minutes": 90,
        "max_slippage_bps": "30",
        "max_1d_il_estimate_pct": 2.5,
        "apy_floor_exit_pct": "35",
        "hard_stop_drawdown_pct": "8",
        "safety_score_floor": 40,
        "current_safety_score": 45,
        "rebalance_trigger_price_move_pct": "1.8",
        "rebalance_trigger_token_drift_pct": 12,
        "max_rebalances_per_day": 4,
        "compound_rewards": True,
        "compound_min_reward_usd": 75,
        "exit_teardown_policy": "ON_RISK_OR_YIELD_DETERIORATION",
        "exit_if_apy_below_floor_hours": 8,
        "exit_if_safety_below_floor": True,
        "exit_on_extreme_volatility_1h_pct": 5,
        "atr_period": 14,
        "price_band_lookback_hours": 24,
    }


@pytest.fixture
def strategy(base_config: dict) -> UsdcCbbtcSlipstreamBaseStrategy:
    return UsdcCbbtcSlipstreamBaseStrategy(
        config=base_config,
        chain="base",
        wallet_address="0x" + "1" * 40,
    )


def _intent_type(intent) -> str:
    return getattr(getattr(intent, "intent_type", None), "value", "")


def test_open_position_when_entry_conditions_pass(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    market = FakeMarket()
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_OPEN"
    assert intent.protocol == "aerodrome_slipstream"
    assert intent.pool == "USDC/CBBTC/100"
    assert Decimal(str(intent.range_lower)) == Decimal(str(intent.range_lower)).to_integral_value()
    assert Decimal(str(intent.range_upper)) == Decimal(str(intent.range_upper)).to_integral_value()


def test_hold_when_entry_volatility_too_high(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    market = FakeMarket(atr_value_percent=Decimal("3.0"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_hold_when_price_near_recent_band_edge(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    market = FakeMarket(cbbtc_price=Decimal("61800"), high_24h=Decimal("62000"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_close_on_extreme_volatility_with_open_position(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy._position_id = "123"
    strategy._range_center = Decimal("60000")
    strategy._peak_reference_price = Decimal("62000")
    market = FakeMarket(atr_value_percent=Decimal("5.5"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_close_on_sustained_apy_deterioration(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy._position_id = "123"
    strategy._range_center = Decimal("60000")
    strategy._apy_below_since = datetime.now(UTC) - timedelta(hours=9)
    market = FakeMarket(fee_apy=Decimal("20"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"


def test_close_on_rebalance_token_drift_trigger(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy._position_id = "123"
    strategy._range_center = Decimal("60000")
    market = FakeMarket(token0_weight=Decimal("0.7"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "LP_CLOSE"
    assert strategy._pending_rebalance_swap is True


def test_hold_when_rebalance_cooldown_active(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy._position_id = "123"
    strategy._range_center = Decimal("60000")
    strategy._last_rebalance_at = datetime.now(UTC)
    market = FakeMarket(token0_weight=Decimal("0.7"))
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_post_close_rebalance_swap_path(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy._pending_rebalance_swap = True
    market = FakeMarket(
        fee_apy=Decimal("180"),
        usdc_balance_usd=Decimal("60000"),
        cbbtc_balance_usd=Decimal("10000"),
        usdc_balance=Decimal("60000"),
        cbbtc_balance=Decimal("0.17"),
    )
    intent = strategy.decide(market)
    assert _intent_type(intent) == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "CBBTC"


def test_entry_swap_path_when_inventory_unbalanced(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    market = FakeMarket(
        usdc_balance_usd=Decimal("30000"),
        cbbtc_balance_usd=Decimal("1000"),
        usdc_balance=Decimal("30000"),
        cbbtc_balance=Decimal("0.016"),
    )
    intent = strategy.decide(market)
    assert _intent_type(intent) == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "CBBTC"


def test_min_deploy_tranche_blocks_dust_open(base_config: dict) -> None:
    cfg = dict(base_config)
    cfg["min_deploy_tranche_usd"] = 100
    strat = UsdcCbbtcSlipstreamBaseStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)
    market = FakeMarket(
        usdc_balance_usd=Decimal("50"),
        cbbtc_balance_usd=Decimal("50"),
        usdc_balance=Decimal("50"),
        cbbtc_balance=Decimal("0.00083"),
    )
    intent = strat.decide(market)
    assert _intent_type(intent) == "HOLD"


@pytest.mark.parametrize("force_action,expected", [("open", "LP_OPEN"), ("rebalance_swap", "SWAP"), ("close", "LP_CLOSE")])
def test_force_actions_cover_non_hold_intents(base_config: dict, force_action: str, expected: str) -> None:
    cfg = dict(base_config)
    cfg["force_action"] = force_action
    cfg["force_position_id"] = "555" if force_action == "close" else None
    strat = UsdcCbbtcSlipstreamBaseStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)

    if force_action == "open":
        market = FakeMarket()
    else:
        market = FakeMarket(
            fee_apy=Decimal("180"),
            usdc_balance_usd=Decimal("60000"),
            cbbtc_balance_usd=Decimal("10000"),
            usdc_balance=Decimal("60000"),
            cbbtc_balance=Decimal("0.17"),
        )

    intent = strat.decide(market)
    assert _intent_type(intent) == expected


def test_compute_range_quantizes_to_tick_spacing(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy.tick_spacing = 100
    lower_tick, upper_tick = strategy._compute_range(Decimal("60000"))
    assert isinstance(lower_tick, int)
    assert isinstance(upper_tick, int)
    assert lower_tick % strategy.tick_spacing == 0
    assert upper_tick % strategy.tick_spacing == 0
    assert upper_tick > lower_tick


def test_force_open_uses_quantized_tick_bounds(base_config: dict) -> None:
    cfg = dict(base_config)
    cfg["force_action"] = "open"
    strat = UsdcCbbtcSlipstreamBaseStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)
    intent = strat.decide(FakeMarket())
    assert Decimal(str(intent.range_lower)) == Decimal(str(intent.range_lower)).to_integral_value()
    assert Decimal(str(intent.range_upper)) == Decimal(str(intent.range_upper)).to_integral_value()
    assert int(Decimal(str(intent.range_lower))) % strat.tick_spacing == 0
    assert int(Decimal(str(intent.range_upper))) % strat.tick_spacing == 0


def test_data_unavailable_returns_hold(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    market = FakeMarket()

    def _raise(_token: str):
        raise PriceUnavailableError("USDC", reason="unavailable")

    market.price = _raise
    intent = strategy.decide(market)
    assert _intent_type(intent) == "HOLD"


def test_persistence_roundtrip(base_config: dict) -> None:
    strat = UsdcCbbtcSlipstreamBaseStrategy(config=base_config, chain="base", wallet_address="0x" + "1" * 40)
    strat._position_id = "999"
    strat._range_lower = 59000
    strat._range_upper = 61000
    strat._range_center = Decimal("60000")
    strat._pending_rebalance_swap = True
    state = strat.get_persistent_state()

    fresh = UsdcCbbtcSlipstreamBaseStrategy(config=base_config, chain="base", wallet_address="0x" + "1" * 40)
    fresh.load_persistent_state(state)
    assert fresh.get_persistent_state()["position_id"] == "999"
    assert fresh.get_persistent_state()["pending_rebalance_swap"] is True


def test_teardown_methods_cover_open_position(strategy: UsdcCbbtcSlipstreamBaseStrategy) -> None:
    strategy._position_id = "1001"
    summary = strategy.get_open_positions()
    assert len(summary.positions) == 1
    intents = strategy.generate_teardown_intents(mode=None)
    assert len(intents) == 1
    assert _intent_type(intents[0]) == "LP_CLOSE"
