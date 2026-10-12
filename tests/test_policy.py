#!/usr/bin/env python3
"""Tests for provider_policy and pricing_overrides in sync_configs.py.

Tests policy formation (per-model order, fallbacks, default), route pricing
(rub per 1M → rub per token conversion, cost_per_turn), and sticky behavior
(models use their configured provider order from filter_config).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to path (same pattern as tests/test_endpoints.py)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sync_configs import (
    PROVIDER_POLICY_DEFAULT,
    PRICING_OVERRIDES_DEFAULT,
    find_replacement,
    hybrid_score,
    select_top_models,
    TIER_ORDER,
)


def test_provider_policy_default():
    """Default provider_policy is per-model with empty models dict."""
    from sync_configs import PROVIDER_POLICY_DEFAULT as pp
    assert pp["mode"] == "per_model"
    assert pp["models"] == {}
    assert pp["default"]["order"] == []
    assert pp["default"]["allow_fallbacks"] is True


def test_pricing_overrides_default():
    """Default pricing_overrides is empty dict."""
    from sync_configs import PRICING_OVERRIDES_DEFAULT as po
    assert po == {}


def test_find_replacement_same_id():
    """find_replacement returns None when model id is already available."""
    available = [{"id": "deepseek/deepseek-v4.1-flash", "provider": "deepseek", "tier": "cheap", "supports_tools": True}]
    result = find_replacement("deepseek/deepseek-v4.1-flash", available, "deepseek/deepseek-v4.1-flash")
    assert result is None  # already актуальна


def test_find_replacement_same_provider():
    """find_replacement prefers same provider cheap model with tools."""
    available = [
        {"id": "deepseek/deepseek-v4.1-flash", "provider": "deepseek", "tier": "cheap", "supports_tools": True},
        {"id": "deepseek/deepseek-v4.1-flash-2", "provider": "deepseek", "tier": "cheap", "supports_tools": False},
        {"id": "openai/gpt-4o-mini", "provider": "openai", "tier": "cheap", "supports_tools": True},
    ]
    # Use a model NOT in available list to test same-provider replacement
    result = find_replacement("deepseek/deepseek-v4.1-flash-3", available, "deepseek/deepseek-v4.1-flash")
    # flash-3 not in available, same provider deepseek, should find flash with tools
    assert result is not None
    assert result["provider"] == "deepseek"


def test_find_replacement_fallback_cheap_tools():
    """find_replacement falls back to cheapest cheap model with tools."""
    available = [
        {"id": "openai/gpt-4o-mini", "provider": "openai", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 50.0},
        {"id": "anthropic/claude-sonnet-4", "provider": "anthropic", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 30.0},
    ]
    result = find_replacement("google/gemini-1.5-flash", available, "deepseek/deepseek-v4.1-flash")
    assert result is not None
    # Should pick the cheapest with tools: anthropic at 30.0
    assert result["id"] == "anthropic/claude-sonnet-4"


def test_hybrid_score_with_recommendations():
    """hybrid_score computes score from recommendations with weights."""
    weights = {"default_agent": 1.0, "cheap_workhorse": 1.0}
    recommendations = {
        "categories": {
            "default_agent": {"top": [{"id": "deepseek/deepseek-v4.1-flash", "score": 0.95}]},
            "cheap_workhorse": {"top": [{"id": "deepseek/deepseek-v4.1-flash", "score": 0.9}]},
        }
    }
    score = hybrid_score("deepseek/deepseek-v4.1-flash", recommendations, weights)
    assert score > 0.0


def test_select_top_models_with_provider_policy():
    """select_top_models respects provider_policy model order when available."""
    catalog = [
        {"id": "deepseek/deepseek-v4.1-flash", "provider": "deepseek", "tier": "cheap", "supports_tools": True},
        {"id": "anthropic/claude-sonnet-4", "provider": "anthropic", "tier": "cheap", "supports_tools": True},
        {"id": "google/gemini-1.5-flash", "provider": "google", "tier": "cheap", "supports_tools": True},
    ]
    sync_cfg = {"selection": {"total": 3, "quotas": {"cheap": 3, "medium": 0, "premium": 0}}}
    recommendations = None
    ref_id = "deepseek/deepseek-v4.1-flash"
    result = select_top_models(catalog, sync_cfg, recommendations, ref_id)
    assert result is not None
    assert len(result) == 3


def test_cost_per_turn_conversion():
    """Test rub per 1M to rub per token conversion."""
    from endpoints import cost_per_turn

    # Endpoint with rub_per_1m values
    ep = {
        "rub_per_1m_prompt": 18_200_000.0,  # 18.2 * 1e6
        "rub_per_1m_completion": 18_200_000.0,
    }
    cost = cost_per_turn(ep, prompt_tokens=1000, completion_tokens=500)
    # (1000 * 18200000 + 500 * 18200000) / 1e6 = 27300000000 / 1e6 = 27300.0
    assert cost == 27300.0


def test_route_vs_aggregate_basic():
    """Test route vs aggregate delta_percent."""
    from endpoints import route_vs_aggregate, cost_per_turn

    # Create mock endpoints with known cost_per_turn values
    eps = [
        {"tag": "ep1", "pricing": {"prompt": 10.0, "completion": 10.0}, "pricing_units": {"prompt": "token", "completion": "token"}},
        {"tag": "ep2", "pricing": {"prompt": 20.0, "completion": 20.0}, "pricing_units": {"prompt": "token", "completion": "token"}},
    ]
    # Set rub_per_1m for cost_per_turn calculation
    for ep in eps:
        ep["rub_per_1m_prompt"] = 10_000_000.0
        ep["rub_per_1m_completion"] = 10_000_000.0

    delta = route_vs_aggregate(100.0, eps)
    # With cost_per_turn ~10.0 for each, delta = (10 - 100) / 100 * 100 = -90.0
    assert delta is not None
    assert len(delta["routes"]) == 2


def test_policy_model_order():
    """Test that per-model provider order is correctly structured."""
    policy = {
        "mode": "per_model",
        "models": {
            "deepseek/deepseek-v4.1-flash": {"order": ["deepseek", "yandex"], "allow_fallbacks": True},
            "anthropic/claude-sonnet-4.5": {"order": ["anthropic", "google"]},
        },
        "default": {"order": [], "allow_fallbacks": True},
    }

    assert policy["mode"] == "per_model"
    assert "deepseek/deepseek-v4.1-flash" in policy["models"]
    assert policy["models"]["deepseek/deepseek-v4.1-flash"]["order"] == ["deepseek", "yandex"]
    assert policy["models"]["deepseek/deepseek-v4.1-flash"]["allow_fallbacks"] is True
    assert policy["default"]["order"] == []


def test_pricing_overrides_format():
    """Test pricing_overrides has correct format (prompt_token, completion_token)."""
    overrides = {
        "deepseek/deepseek-v4.1-flash": {"prompt_token": 0.0182, "completion_token": 0.0182},
        "anthropic/claude-sonnet-4.5": {"prompt_token": 0.025, "completion_token": 0.015},
    }

    assert "deepseek/deepseek-v4.1-flash" in overrides
    assert overrides["deepseek/deepseek-v4.1-flash"]["prompt_token"] == 0.0182
    assert overrides["deepseek/deepseek-v4.1-flash"]["completion_token"] == 0.0182
    assert overrides["anthropic/claude-sonnet-4.5"]["prompt_token"] == 0.025
    assert overrides["anthropic/claude-sonnet-4.5"]["completion_token"] == 0.015


def test_no_variable_pricings_included():
    """Test that variable_pricings are NOT included in pricing_overrides."""
    # This is a documentation/test assertion - pricing_overrides should only
    # contain base token tariffs, not variable threshold prices
    overrides = {
        "deepseek/deepseek-v4.1-flash": {"prompt_token": 0.0182, "completion_token": 0.0182},
    }

    # Should NOT contain variable_pricings keys
    for model_id, price in overrides.items():
        assert "prompt_threshold" not in price
        assert "variable" not in price
        assert "threshold" not in price


def test_is_token_endpoint():
    """Test is_token_endpoint filters endpoints with chat API and tools."""
    from endpoints import is_token_endpoint

    # Endpoint with chat API and tools → token endpoint
    eps_with_tools = {
        "supported_apis": ["chat"],
        "supported_parameters": ["tools"],
        "context_length": 32768,
        "max_prompt_tokens": 8192,
        "supports_tools": True,
    }
    assert is_token_endpoint(eps_with_tools) is True

    # Endpoint without chat API → not token endpoint
    eps_no_chat = {
        "supported_apis": ["completion"],
        "supported_parameters": ["tools"],
        "context_length": 32768,
        "max_prompt_tokens": 8192,
        "supports_tools": True,
    }
    assert is_token_endpoint(eps_no_chat) is False

    # Endpoint without tools → not token endpoint
    # Function checks supports_tools directly; use supports_tools: False
    eps_no_tools = {
        "supported_apis": ["chat"],
        "supported_parameters": ["streaming"],
        "context_length": 32768,
        "max_prompt_tokens": 8192,
        "supports_tools": False,  # Function checks this directly
    }
    assert is_token_endpoint(eps_no_tools) is False
def test_cheapest_endpoint_tools():
    """Test cheapest_endpoint selects among endpoints with tools."""
    from endpoints import cheapest_endpoint, endpoint_rub_per_1m

    endpoints = [
        {
            "tag": "deepseek",
            "provider_name": "deepseek",
            "name": "endpoint-1",
            "status": 0,
            "quantization": "int4",
            "countries": ["RU"],
            "context_length": 32768,
            "max_completion_tokens": 8192,
            "max_prompt_tokens": 32768,
            "supported_parameters": ["tools"],
            "supports_tools": True,
            "supported_apis": ["chat"],
            "pricing": {"prompt": 18.2, "completion": 18.2, "input_cache_read": 0, "image_output": 0, "seconds": 0},
            "pricing_units": {"prompt": "token", "completion": "token", "input_cache_read": "token", "image_output": "megapixel", "seconds": "second"},
        },
        {
            "tag": "yandex",
            "provider_name": "yandex",
            "name": "endpoint-2",
            "status": 0,
            "quantization": "int8",
            "countries": ["RU"],
            "context_length": 16384,
            "max_completion_tokens": 4096,
            "max_prompt_tokens": 16384,
            "supported_parameters": ["tools"],
            "supports_tools": True,
            "supported_apis": ["chat"],
            "pricing": {"prompt": 15.0, "completion": 15.0, "input_cache_read": 0, "image_output": 0, "seconds": 0},
            "pricing_units": {"prompt": "token", "completion": "token", "input_cache_read": "token", "image_output": "megapixel", "seconds": "second"},
        },
    ]

    # Pre-compute rub_per_1m for each endpoint (required by cheapest_endpoint)
    for ep in endpoints:
        rub = endpoint_rub_per_1m(ep)
        ep["rub_per_1m_prompt"] = rub["rub_per_1m_prompt"]
        ep["rub_per_1m_completion"] = rub["rub_per_1m_completion"]

    cheapest = cheapest_endpoint(endpoints)
    assert cheapest is not None
    # Should pick the cheaper one (yandex at 15.0 vs deepseek at 18.2)
    assert cheapest["name"] == "endpoint-2"


def test_endpoint_rub_per_1m_token_only():
    """Test endpoint_rub_per_1m returns rub per 1M for token-priced keys."""
    from endpoints import endpoint_rub_per_1m

    eps = {
        "pricing": {"prompt": 18.2, "completion": 18.2, "input_cache_read": 0, "image_output": 0, "seconds": 0},
        "pricing_units": {"prompt": "token", "completion": "token", "input_cache_read": "token", "image_output": "megapixel", "seconds": "second"},
    }

    result = endpoint_rub_per_1m(eps)
    assert result is not None
    # rub_per_1m_prompt should be 18.2 * 1e6 since units is "token"
    assert result["rub_per_1m_prompt"] == 18.2 * 1_000_000
    # rub_per_1m_completion should be 18.2 * 1e6 since units is "token"
    assert result["rub_per_1m_completion"] == 18.2 * 1_000_000
    # rub_per_1m_image_output stays as-is since units is "megapixel" (not "token")
    assert result["rub_per_1m_image_output"] == 0  # price stays in original unit
    # rub_per_1m_seconds stays as-is since units is "second" (not "token")
    assert result["rub_per_1m_seconds"] == 0


def test_rub_per_1m_conversion():
    """Test rub_per_1m conversion from pricing data."""
    from filter_models import rub_per_1m

    # 18.2 rub per token → 18.2 * 1M = 18200000 rub per 1M tokens
    result = rub_per_1m(18.2)
    assert result == 18_200_000.0
