from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from almanak.framework.data import (
    MarketSnapshotError,
    PoolAnalyticsUnavailableError,
    PriceUnavailableError,
)
from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="usdc_cbbtc_slipstream_base",
    description="Aerodrome Slipstream USDC-CBBTC LP yield strategy",
    version="1.0.0",
    author="Almanak",
    tags=["lp", "yield", "aerodrome_slipstream", "base"],
    supported_chains=["base"],
    supported_protocols=["aerodrome_slipstream"],
    intent_types=["LP_OPEN", "LP_CLOSE", "SWAP", "HOLD"],
    default_chain="base",
    quote_asset="USD",
)
class UsdcCbbtcSlipstreamBaseStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        def cfg(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        self.protocol = cfg("protocol", "aerodrome_slipstream")
        self.pool_name = cfg("pool_name", "USDC-CBBTC")
        self.pool = cfg("pool", "USDC/CBBTC/100")
        self.pool_address = cfg("pool_address", "")
        pool_parts = [part.strip() for part in str(self.pool).split("/") if part.strip()]
        self.pool_token0 = pool_parts[0].upper() if len(pool_parts) >= 1 else "USDC"
        self.pool_token1 = pool_parts[1].upper() if len(pool_parts) >= 2 else "CBBTC"
        self.tick_spacing = int(pool_parts[2]) if len(pool_parts) >= 3 else 100

        self.force_action = str(cfg("force_action", "") or "").strip().lower()
        self.force_position_id = cfg("force_position_id", None)

        self.max_capital_deploy_pct = Decimal(str(cfg("max_capital_deploy_pct", "80")))
        self.capital_reserve_pct = Decimal(str(cfg("capital_reserve_pct", "20")))
        self.initial_tranche_pct = Decimal(str(cfg("initial_tranche_pct", "50")))
        self.followup_tranche_pct = Decimal(str(cfg("followup_tranche_pct", "30")))
        self.min_deploy_tranche_usd = Decimal(str(cfg("min_deploy_tranche_usd", "25")))

        self.entry_band_pct = Decimal(str(cfg("entry_require_price_within_recent_band_pct", "1.2")))
        self.entry_max_vol_pct = Decimal(str(cfg("entry_max_volatility_1h_pct", "2")))
        self.cooldown_after_position_change_minutes = int(
            cfg("cooldown_after_position_change_minutes", cfg("cooldown_after_rebalance_minutes", 90))
        )

        self.max_slippage_bps = Decimal(str(cfg("max_slippage_bps", "30")))
        self.max_1d_il_estimate_pct = Decimal(str(cfg("max_1d_il_estimate_pct", "2.5")))
        self.apy_floor_exit_pct = Decimal(str(cfg("apy_floor_exit_pct", "35")))
        self.hard_stop_drawdown_pct = Decimal(str(cfg("hard_stop_drawdown_pct", "8")))
        self.safety_score_floor = Decimal(str(cfg("safety_score_floor", "40")))
        self.current_safety_score = Decimal(str(cfg("current_safety_score", "45")))

        self.rebalance_trigger_price_move_pct = Decimal(str(cfg("rebalance_trigger_price_move_pct", "1.8")))
        self.rebalance_trigger_token_drift_pct = Decimal(str(cfg("rebalance_trigger_token_drift_pct", "12")))
        self.max_rebalances_per_day = int(cfg("max_rebalances_per_day", 4))

        self.compound_rewards = bool(cfg("compound_rewards", True))
        self.compound_min_reward_usd = Decimal(str(cfg("compound_min_reward_usd", "75")))

        self.exit_teardown_policy = cfg("exit_teardown_policy", "ON_RISK_OR_YIELD_DETERIORATION")
        self.exit_if_apy_below_floor_hours = int(cfg("exit_if_apy_below_floor_hours", 8))
        self.exit_if_safety_below_floor = bool(cfg("exit_if_safety_below_floor", True))
        self.exit_on_extreme_volatility_1h_pct = Decimal(str(cfg("exit_on_extreme_volatility_1h_pct", "5")))

        self.atr_period = int(cfg("atr_period", 14))
        self.price_band_lookback_hours = int(cfg("price_band_lookback_hours", 24))

        self._position_id: str | None = None
        self._range_lower: int | None = None
        self._range_upper: int | None = None
        self._range_center: Decimal | None = None
        self._last_rebalance_at: datetime | None = None
        self._last_position_change_at: datetime | None = None
        self._rebalances_today = 0
        self._rebalances_day_key = date.today().isoformat()
        self._apy_below_since: datetime | None = None
        self._deploy_stage = 1
        self._pending_rebalance_swap = False
        self._peak_reference_price: Decimal | None = None
        self._last_open_at: datetime | None = None

    def decide(self, market: MarketSnapshot) -> Intent:
        if self.force_action:
            return self._forced_intent(market)

        now = datetime.now(UTC)

        try:
            usdc_price = market.price("USDC")
            cbbtc_price = market.price("CBBTC")
            pool_analytics = market.pool_analytics(
                self.pool_address,
                protocol=self.protocol,
                chain=self.chain,
            )
            atr_1h = market.atr("CBBTC", period=self.atr_period, timeframe="1h")
            price_data = market.price_data("CBBTC")
            projected_il = market.projected_il(
                "CBBTC",
                "USDC",
                price_change_pct=Decimal(str(getattr(atr_1h, "value_percent", Decimal("0")))),
            )
        except (
            PriceUnavailableError,
            PoolAnalyticsUnavailableError,
            MarketSnapshotError,
            ValueError,
            KeyError,
        ) as exc:
            return Intent.hold(reason=f"market data unavailable: {exc}")

        fee_apy = Decimal(str(getattr(pool_analytics.value, "fee_apy", "0")))
        token0_weight = Decimal(str(getattr(pool_analytics.value, "token0_weight", "0.5")))
        token_drift_pct = abs(token0_weight - Decimal("0.5")) * Decimal("200")
        vol_1h_pct = Decimal(str(getattr(atr_1h, "value_percent", "0")))
        il_pct = Decimal(str(getattr(projected_il, "il_percent", "0")))

        pool_price = self._pool_price(usdc_price=usdc_price, cbbtc_price=cbbtc_price)
        if pool_price is None:
            return Intent.hold(reason="invalid pool price")

        recent_low = Decimal(str(getattr(price_data, "low_24h", cbbtc_price)))
        recent_high = Decimal(str(getattr(price_data, "high_24h", cbbtc_price)))
        distance_to_low_pct = self._distance_pct(cbbtc_price, recent_low)
        distance_to_high_pct = self._distance_pct(recent_high, cbbtc_price)

        if self._position_id:
            return self._manage_open_position(
                now=now,
                cbbtc_price=cbbtc_price,
                pool_price=pool_price,
                fee_apy=fee_apy,
                vol_1h_pct=vol_1h_pct,
                il_pct=il_pct,
                token_drift_pct=token_drift_pct,
            )

        if self._pending_rebalance_swap:
            swap = self._rebalance_inventory_swap(market, usdc_price, cbbtc_price, fee_apy)
            if swap is not None:
                return swap

        if self._is_cooldown_active(now):
            return Intent.hold(reason="cooldown after position change")
        band_window_scale = Decimal("24") / Decimal(str(max(self.price_band_lookback_hours, 1)))
        configured_band_distance = self.entry_band_pct * band_window_scale
        observed_band_pct = Decimal("0")
        if cbbtc_price > 0:
            observed_band_pct = abs(recent_high - recent_low) / cbbtc_price * Decimal("100")
        max_feasible_band_distance = max((observed_band_pct / Decimal("2")) - Decimal("0.01"), Decimal("0"))
        required_band_distance = min(configured_band_distance, max_feasible_band_distance)
        if required_band_distance > 0 and (
            distance_to_low_pct < required_band_distance or distance_to_high_pct < required_band_distance
        ):
            return Intent.hold(reason="price too close to recent band edge")
        if vol_1h_pct > self.entry_max_vol_pct:
            return Intent.hold(reason="entry volatility too high")
        if fee_apy < self.apy_floor_exit_pct:
            return Intent.hold(reason="apy below floor")
        if self.exit_if_safety_below_floor and self.current_safety_score < self.safety_score_floor:
            return Intent.hold(reason="safety score below floor")
        if il_pct > self.max_1d_il_estimate_pct:
            return Intent.hold(reason="projected IL too high")

        try:
            usdc_balance = market.balance("USDC", price=usdc_price)
            cbbtc_balance = market.balance("CBBTC", price=cbbtc_price)
        except (MarketSnapshotError, ValueError, KeyError) as exc:
            return Intent.hold(reason=f"balance unavailable: {exc}")

        usdc_balance_usd = Decimal(str(usdc_balance.balance_usd))
        cbbtc_balance_usd = Decimal(str(cbbtc_balance.balance_usd))
        sizes = self._calculate_open_sizes(
            usdc_balance_usd=usdc_balance_usd,
            cbbtc_balance_usd=cbbtc_balance_usd,
            usdc_price=usdc_price,
            cbbtc_price=cbbtc_price,
            usdc_balance=Decimal(str(usdc_balance.balance)),
            cbbtc_balance=Decimal(str(cbbtc_balance.balance)),
        )
        if sizes is None:
            entry_swap = self._entry_inventory_swap(usdc_balance_usd=usdc_balance_usd, cbbtc_balance_usd=cbbtc_balance_usd)
            if entry_swap is not None:
                return entry_swap
            return Intent.hold(reason="insufficient balanced capital for open")

        range_lower_tick, range_upper_tick = self._compute_range(pool_price)
        self._range_lower = range_lower_tick
        self._range_upper = range_upper_tick
        self._range_center = pool_price

        return Intent.lp_open(
            pool=self.pool,
            amount0=sizes["usdc_amount"],
            amount1=sizes["cbbtc_amount"],
            range_lower=range_lower_tick,
            range_upper=range_upper_tick,
            protocol=self.protocol,
            chain=self.chain,
        )

    def _manage_open_position(
        self,
        *,
        now: datetime,
        cbbtc_price: Decimal,
        pool_price: Decimal,
        fee_apy: Decimal,
        vol_1h_pct: Decimal,
        il_pct: Decimal,
        token_drift_pct: Decimal,
    ) -> Intent:
        if self._peak_reference_price is None:
            self._peak_reference_price = cbbtc_price
        else:
            self._peak_reference_price = max(self._peak_reference_price, cbbtc_price)

        drawdown_pct = Decimal("0")
        if self._peak_reference_price > 0:
            drawdown_pct = (self._peak_reference_price - cbbtc_price) / self._peak_reference_price * Decimal("100")

        if self.exit_teardown_policy == "ON_RISK_OR_YIELD_DETERIORATION":
            if vol_1h_pct >= self.exit_on_extreme_volatility_1h_pct:
                return self._close_intent("extreme volatility")
            if drawdown_pct >= self.hard_stop_drawdown_pct:
                return self._close_intent("hard stop drawdown")
            if self.exit_if_safety_below_floor and self.current_safety_score < self.safety_score_floor:
                return self._close_intent("safety floor breached")
            if il_pct > self.max_1d_il_estimate_pct:
                return self._close_intent("projected IL breached")

        if fee_apy < self.apy_floor_exit_pct:
            if self._apy_below_since is None:
                self._apy_below_since = now
            below_for = now - self._apy_below_since
            if below_for >= timedelta(hours=self.exit_if_apy_below_floor_hours):
                return self._close_intent("apy below floor sustained")
        else:
            self._apy_below_since = None

        price_move_from_center_pct = Decimal("0")
        if self._range_center and self._range_center > 0:
            price_move_from_center_pct = abs(pool_price - self._range_center) / self._range_center * Decimal("100")

        if (
            price_move_from_center_pct >= self.rebalance_trigger_price_move_pct
            or token_drift_pct >= self.rebalance_trigger_token_drift_pct
        ):
            if self._is_cooldown_active(now):
                return Intent.hold(reason="rebalance trigger hit, cooldown active")
            if not self._can_rebalance_today(now):
                return Intent.hold(reason="rebalance limit reached for today")
            self._pending_rebalance_swap = True
            return self._close_intent("rebalance trigger")

        return Intent.hold(reason="position healthy")

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "open":
            usdc_price = market.price("USDC")
            cbbtc_price = market.price("CBBTC")
            usdc_balance = market.balance("USDC", price=usdc_price)
            cbbtc_balance = market.balance("CBBTC", price=cbbtc_price)
            sizes = self._calculate_open_sizes(
                usdc_balance_usd=Decimal(str(usdc_balance.balance_usd)),
                cbbtc_balance_usd=Decimal(str(cbbtc_balance.balance_usd)),
                usdc_price=usdc_price,
                cbbtc_price=cbbtc_price,
                usdc_balance=Decimal(str(usdc_balance.balance)),
                cbbtc_balance=Decimal(str(cbbtc_balance.balance)),
            )
            if sizes is None:
                raise ValueError("force_action=open requires sufficient balances")
            pool_price = self._pool_price(usdc_price=usdc_price, cbbtc_price=cbbtc_price)
            if pool_price is None:
                raise ValueError("force_action=open could not derive pool price")
            lower_tick, upper_tick = self._compute_range(pool_price)
            return Intent.lp_open(
                pool=self.pool,
                amount0=sizes["usdc_amount"],
                amount1=sizes["cbbtc_amount"],
                range_lower=lower_tick,
                range_upper=upper_tick,
                protocol=self.protocol,
                chain=self.chain,
            )

        if self.force_action == "rebalance_swap":
            usdc_price = market.price("USDC")
            cbbtc_price = market.price("CBBTC")
            swap = self._rebalance_inventory_swap(
                market,
                usdc_price,
                cbbtc_price,
                fee_apy=Decimal(str(self.apy_floor_exit_pct)),
                force=True,
            )
            if swap is None:
                raise ValueError("force_action=rebalance_swap requires skewed balances")
            return swap

        if self.force_action == "close":
            position_id = str(self.force_position_id or self._position_id or "")
            if not position_id:
                raise ValueError("force_action=close requires force_position_id or tracked position")
            return Intent.lp_close(
                position_id=position_id,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.chain,
            )

        raise ValueError(f"Unknown force_action: {self.force_action}")

    def _compute_range(self, mid_price: Decimal) -> tuple[int, int]:
        half_width = self.rebalance_trigger_price_move_pct / Decimal("100")
        raw_lower = mid_price * (Decimal("1") - half_width)
        raw_upper = mid_price * (Decimal("1") + half_width)

        lower_tick = self._price_to_tick(raw_lower)
        upper_tick = self._price_to_tick(raw_upper)

        spacing = max(int(self.tick_spacing), 1)
        snapped_lower_tick = (lower_tick // spacing) * spacing
        snapped_upper_tick = ((upper_tick + spacing - 1) // spacing) * spacing
        if snapped_upper_tick <= snapped_lower_tick:
            snapped_upper_tick = snapped_lower_tick + spacing

        return snapped_lower_tick, snapped_upper_tick

    def _price_to_tick(self, price: Decimal) -> int:
        if price <= 0:
            raise ValueError("price must be positive to derive tick")
        tick = math.log(float(price)) / math.log(1.0001)
        return int(Decimal(str(tick)).to_integral_value(rounding=ROUND_FLOOR))

    def _tick_to_price(self, tick: int) -> Decimal:
        return Decimal(str(math.pow(1.0001, tick)))

    def _calculate_open_sizes(
        self,
        *,
        usdc_balance_usd: Decimal,
        cbbtc_balance_usd: Decimal,
        usdc_price: Decimal,
        cbbtc_price: Decimal,
        usdc_balance: Decimal,
        cbbtc_balance: Decimal,
    ) -> dict[str, Decimal] | None:
        total_usd = usdc_balance_usd + cbbtc_balance_usd
        if total_usd <= 0:
            return None

        reserve_usd = total_usd * self.capital_reserve_pct / Decimal("100")
        deploy_cap_usd = total_usd * self.max_capital_deploy_pct / Decimal("100")
        deployable_usd = min(total_usd - reserve_usd, deploy_cap_usd)
        tranche_pct = self.initial_tranche_pct if self._deploy_stage == 1 else self.followup_tranche_pct
        tranche_usd = deployable_usd * tranche_pct / Decimal("100")
        side_usd = tranche_usd / Decimal("2")

        if tranche_usd < self.min_deploy_tranche_usd:
            return None
        if side_usd <= 0 or usdc_price <= 0 or cbbtc_price <= 0:
            return None
        if usdc_balance_usd < side_usd or cbbtc_balance_usd < side_usd:
            return None

        usdc_amount = min(side_usd / usdc_price, usdc_balance)
        cbbtc_amount = min(side_usd / cbbtc_price, cbbtc_balance)
        if usdc_amount <= 0 or cbbtc_amount <= 0:
            return None

        return {
            "usdc_amount": usdc_amount,
            "cbbtc_amount": cbbtc_amount,
        }

    def _entry_inventory_swap(self, *, usdc_balance_usd: Decimal, cbbtc_balance_usd: Decimal) -> Intent | None:
        total_usd = usdc_balance_usd + cbbtc_balance_usd
        if total_usd <= 0:
            return None

        reserve_usd = total_usd * self.capital_reserve_pct / Decimal("100")
        deploy_cap_usd = total_usd * self.max_capital_deploy_pct / Decimal("100")
        deployable_usd = min(total_usd - reserve_usd, deploy_cap_usd)
        tranche_usd = deployable_usd * self.initial_tranche_pct / Decimal("100")
        if tranche_usd < self.min_deploy_tranche_usd:
            return None

        side_usd = tranche_usd / Decimal("2")
        max_swap_usd = total_usd * Decimal("0.45")
        swap_tolerance_usd = Decimal("1")

        if usdc_balance_usd + swap_tolerance_usd < side_usd and cbbtc_balance_usd > side_usd:
            amount_usd = min(side_usd - usdc_balance_usd, max_swap_usd)
            if amount_usd > swap_tolerance_usd:
                return Intent.swap(
                    from_token="CBBTC",
                    to_token="USDC",
                    amount_usd=amount_usd,
                    max_slippage=self.max_slippage_bps / Decimal("10000"),
                    protocol=self.protocol,
                    chain=self.chain,
                )

        if cbbtc_balance_usd + swap_tolerance_usd < side_usd and usdc_balance_usd > side_usd:
            amount_usd = min(side_usd - cbbtc_balance_usd, max_swap_usd)
            if amount_usd > swap_tolerance_usd:
                return Intent.swap(
                    from_token="USDC",
                    to_token="CBBTC",
                    amount_usd=amount_usd,
                    max_slippage=self.max_slippage_bps / Decimal("10000"),
                    protocol=self.protocol,
                    chain=self.chain,
                )

        return None

    def _rebalance_inventory_swap(
        self,
        market: MarketSnapshot,
        usdc_price: Decimal,
        cbbtc_price: Decimal,
        fee_apy: Decimal,
        force: bool = False,
    ) -> Intent | None:
        usdc_balance = market.balance("USDC", price=usdc_price)
        cbbtc_balance = market.balance("CBBTC", price=cbbtc_price)
        usdc_usd = Decimal(str(usdc_balance.balance_usd))
        cbbtc_usd = Decimal(str(cbbtc_balance.balance_usd))
        total_usd = usdc_usd + cbbtc_usd
        if total_usd <= 0:
            self._pending_rebalance_swap = False
            return None

        estimated_daily_reward = total_usd * fee_apy / Decimal("100") / Decimal("365")
        if self.compound_rewards and not force and estimated_daily_reward < self.compound_min_reward_usd:
            return Intent.hold(reason="rewards below compound threshold")

        half = total_usd / Decimal("2")
        skew = usdc_usd - half
        tolerance = total_usd * Decimal("0.05")
        if abs(skew) <= tolerance:
            self._pending_rebalance_swap = False
            return None

        amount_usd = abs(skew)
        self._pending_rebalance_swap = False
        if skew > 0:
            return Intent.swap(
                from_token="USDC",
                to_token="CBBTC",
                amount_usd=amount_usd,
                max_slippage=self.max_slippage_bps / Decimal("10000"),
                protocol=self.protocol,
                chain=self.chain,
            )
        return Intent.swap(
            from_token="CBBTC",
            to_token="USDC",
            amount_usd=amount_usd,
            max_slippage=self.max_slippage_bps / Decimal("10000"),
            protocol=self.protocol,
            chain=self.chain,
        )

    def _close_intent(self, reason: str) -> Intent:
        if not self._position_id:
            return Intent.hold(reason="close requested with no position")
        logger.info("closing position %s: %s", self._position_id, reason)
        return Intent.lp_close(
            position_id=self._position_id,
            pool=self.pool,
            collect_fees=True,
            protocol=self.protocol,
            chain=self.chain,
        )

    def _distance_pct(self, a: Decimal, b: Decimal) -> Decimal:
        if b <= 0:
            return Decimal("0")
        return abs(a - b) / b * Decimal("100")

    def _safe_ratio(self, numerator: Decimal, denominator: Decimal) -> Decimal | None:
        if denominator <= 0:
            return None
        return numerator / denominator

    def _pool_price(self, *, usdc_price: Decimal, cbbtc_price: Decimal) -> Decimal | None:
        prices = {
            "USDC": usdc_price,
            "CBBTC": cbbtc_price,
        }
        token0_price = prices.get(self.pool_token0)
        token1_price = prices.get(self.pool_token1)
        if token0_price is None or token1_price is None:
            return None
        return self._safe_ratio(token0_price, token1_price)

    def _is_cooldown_active(self, now: datetime) -> bool:
        if self._last_position_change_at is None:
            return False
        return now - self._last_position_change_at < timedelta(minutes=self.cooldown_after_position_change_minutes)

    def _can_rebalance_today(self, now: datetime) -> bool:
        day_key = now.date().isoformat()
        if day_key != self._rebalances_day_key:
            self._rebalances_day_key = day_key
            self._rebalances_today = 0
        if self._rebalances_today >= self.max_rebalances_per_day:
            return False
        self._rebalances_today += 1
        self._last_rebalance_at = now
        return True

    def on_intent_executed(self, intent: Intent, success: bool, result: Any) -> None:
        if not success:
            return

        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")
        if intent_type == "LP_OPEN":
            position_id = getattr(result, "position_id", None)
            if position_id is not None:
                self._position_id = str(position_id)
            self._range_lower = int(getattr(intent, "range_lower", 0))
            self._range_upper = int(getattr(intent, "range_upper", 0))
            center_tick = (self._range_lower + self._range_upper) // 2
            self._range_center = self._tick_to_price(center_tick)
            self._last_open_at = datetime.now(UTC)
            self._last_position_change_at = self._last_open_at
            self._peak_reference_price = self._range_center
            self._apy_below_since = None
            self._pending_rebalance_swap = False
            if self._deploy_stage == 1:
                self._deploy_stage = 2

        elif intent_type == "LP_CLOSE":
            self._position_id = None
            self._range_lower = None
            self._range_upper = None
            self._range_center = None
            self._peak_reference_price = None
            self._last_position_change_at = datetime.now(UTC)

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "position_id": self._position_id,
            "range_lower": str(self._range_lower) if self._range_lower is not None else None,
            "range_upper": str(self._range_upper) if self._range_upper is not None else None,
            "range_center": str(self._range_center) if self._range_center is not None else None,
            "last_rebalance_at": self._last_rebalance_at.isoformat() if self._last_rebalance_at else None,
            "last_position_change_at": self._last_position_change_at.isoformat()
            if self._last_position_change_at
            else None,
            "rebalances_today": self._rebalances_today,
            "rebalances_day_key": self._rebalances_day_key,
            "apy_below_since": self._apy_below_since.isoformat() if self._apy_below_since else None,
            "deploy_stage": self._deploy_stage,
            "pending_rebalance_swap": self._pending_rebalance_swap,
            "peak_reference_price": str(self._peak_reference_price) if self._peak_reference_price is not None else None,
            "last_open_at": self._last_open_at.isoformat() if self._last_open_at else None,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return
        self._position_id = state.get("position_id")
        self._range_lower = int(state["range_lower"]) if state.get("range_lower") else None
        self._range_upper = int(state["range_upper"]) if state.get("range_upper") else None
        self._range_center = Decimal(state["range_center"]) if state.get("range_center") else None
        self._last_rebalance_at = datetime.fromisoformat(state["last_rebalance_at"]) if state.get("last_rebalance_at") else None
        self._last_position_change_at = (
            datetime.fromisoformat(state["last_position_change_at"])
            if state.get("last_position_change_at")
            else self._last_rebalance_at
        )
        self._rebalances_today = int(state.get("rebalances_today", 0))
        self._rebalances_day_key = state.get("rebalances_day_key", date.today().isoformat())
        self._apy_below_since = datetime.fromisoformat(state["apy_below_since"]) if state.get("apy_below_since") else None
        self._deploy_stage = int(state.get("deploy_stage", 1))
        self._pending_rebalance_swap = bool(state.get("pending_rebalance_swap", False))
        self._peak_reference_price = (
            Decimal(state["peak_reference_price"]) if state.get("peak_reference_price") else None
        )
        self._last_open_at = datetime.fromisoformat(state["last_open_at"]) if state.get("last_open_at") else None

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions: list[PositionInfo] = []
        if self._position_id:
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=self._position_id,
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=Decimal("0"),
                    details={"pool": self.pool, "pool_name": self.pool_name},
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", "usdc-cbbtc-slipstream-base"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode, market=None) -> list[Intent]:
        if not self._position_id:
            return []
        return [
            Intent.lp_close(
                position_id=self._position_id,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.chain,
            )
        ]
