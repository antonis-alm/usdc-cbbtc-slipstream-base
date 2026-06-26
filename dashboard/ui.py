"""USDC-CBBTC Slipstream Base dashboard."""

from typing import Any

try:
    import streamlit as st
except ModuleNotFoundError:
    class _StreamlitFallback:
        @staticmethod
        def title(_: str) -> None:
            return None

    st = _StreamlitFallback()

from almanak.framework.dashboard.templates import (
    get_aerodrome_config,
    prepare_lp_session_state,
    render_lp_dashboard,
)


def _parse_pool_tokens(pool: str) -> tuple[str, str]:
    parts = [part.strip() for part in pool.split("/") if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "USDC", "CBBTC"


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title("USDC-CBBTC Slipstream (Base)")

    token0, token1 = _parse_pool_tokens(str(strategy_config.get("pool", "USDC/CBBTC/100")))
    config = get_aerodrome_config(
        token0=token0,
        token1=token1,
        pool_type=str(strategy_config.get("pool_type", "volatile")),
        chain=str(strategy_config.get("chain", "base")),
        timeframe=str(strategy_config.get("timeframe", "1h")),
    )

    session_state = prepare_lp_session_state(
        api_client,
        session_state=session_state,
        config=config,
        deployment_id=deployment_id,
    )
    render_lp_dashboard(deployment_id, strategy_config, session_state, config, api_client=api_client)
