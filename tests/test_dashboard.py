from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass


@dataclass
class _FakeConfig:
    protocol: str = "aerodrome"


def _load_ui_module():
    fake_streamlit = types.ModuleType("streamlit")

    def _streamlit_getattr(name: str):
        if name == "cache_data":
            return lambda *_args, **_kwargs: (lambda func: func)
        return lambda *_args, **_kwargs: None

    fake_streamlit.__getattr__ = _streamlit_getattr
    fake_streamlit.title = lambda *_args, **_kwargs: None
    sys.modules["streamlit"] = fake_streamlit

    fake_plotly = types.ModuleType("plotly")
    fake_graph_objects = types.ModuleType("plotly.graph_objects")
    fake_graph_objects.Figure = type("Figure", (), {})
    fake_plotly_express = types.ModuleType("plotly.express")
    fake_plotly_subplots = types.ModuleType("plotly.subplots")
    fake_plotly_subplots.make_subplots = lambda *_args, **_kwargs: None

    fake_plotly.graph_objects = fake_graph_objects
    fake_plotly.express = fake_plotly_express
    fake_plotly.subplots = fake_plotly_subplots

    sys.modules["plotly"] = fake_plotly
    sys.modules["plotly.graph_objects"] = fake_graph_objects
    sys.modules["plotly.express"] = fake_plotly_express
    sys.modules["plotly.subplots"] = fake_plotly_subplots

    if "dashboard.ui" in sys.modules:
        return importlib.reload(sys.modules["dashboard.ui"])
    return importlib.import_module("dashboard.ui")


def test_parse_pool_tokens() -> None:
    ui = _load_ui_module()
    assert ui._parse_pool_tokens("USDC/CBBTC/100") == ("USDC", "CBBTC")
    assert ui._parse_pool_tokens("USDC/CBBTC") == ("USDC", "CBBTC")
    assert ui._parse_pool_tokens("BAD") == ("USDC", "CBBTC")


def test_render_custom_dashboard_uses_lp_template(monkeypatch) -> None:
    ui = _load_ui_module()
    captured: dict[str, object] = {}
    fake_config = _FakeConfig()

    def fake_title(value: str) -> None:
        captured["title"] = value

    def fake_get_aerodrome_config(**kwargs):
        captured["template_kwargs"] = kwargs
        return fake_config

    def fake_prepare_lp_session_state(api_client, session_state, config, deployment_id):
        captured["prepare_args"] = {
            "api_client": api_client,
            "session_state": session_state,
            "config": config,
            "deployment_id": deployment_id,
        }
        return {"prepared": True}

    def fake_render_lp_dashboard(deployment_id, strategy_config, session_state, config, api_client=None):
        captured["render_args"] = {
            "deployment_id": deployment_id,
            "strategy_config": strategy_config,
            "session_state": session_state,
            "config": config,
            "api_client": api_client,
        }

    monkeypatch.setattr(ui.st, "title", fake_title)
    monkeypatch.setattr(ui, "get_aerodrome_config", fake_get_aerodrome_config)
    monkeypatch.setattr(ui, "prepare_lp_session_state", fake_prepare_lp_session_state)
    monkeypatch.setattr(ui, "render_lp_dashboard", fake_render_lp_dashboard)

    strategy_config = {
        "pool": "USDC/CBBTC/100",
        "chain": "base",
        "pool_type": "volatile",
        "timeframe": "1h",
    }
    api_client = object()
    session_state = {"x": 1}

    ui.render_custom_dashboard("dep-1", strategy_config, api_client, session_state)

    assert captured["title"] == "USDC-CBBTC Slipstream (Base)"
    assert captured["template_kwargs"] == {
        "token0": "USDC",
        "token1": "CBBTC",
        "pool_type": "volatile",
        "chain": "base",
        "timeframe": "1h",
    }
    assert captured["prepare_args"] == {
        "api_client": api_client,
        "session_state": session_state,
        "config": fake_config,
        "deployment_id": "dep-1",
    }
    assert captured["render_args"] == {
        "deployment_id": "dep-1",
        "strategy_config": strategy_config,
        "session_state": {"prepared": True},
        "config": fake_config,
        "api_client": api_client,
    }
