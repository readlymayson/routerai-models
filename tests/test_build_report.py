"""Phase 2: тесты build_report.py — route data в normalize_model и каталоге.

Покрывают:
- normalize_model без endpoints_data → routes=[] (backward compatibility);
- normalize_model с endpoints_data → routes содержит delta_percent;
- normalize_model с cheapest endpoint → cheapest_cost_per_turn_estimate_rub заполнено;
- normalize_model без token endpoint → route_pricing_basis=None;
- build_config_format → _meta.routes присутствует для моделей с endpoint'ами;
- catalog.json entries содержат routes после fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from build_report import (  # noqa: E402
    _cheapest_endpoint,
    _cost_per_turn,
    _is_token_endpoint,
    build_config_format,
    normalize_model,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_entry() -> dict[str, Any]:
    """Базовая модель из каталога."""
    return {
        "id": "deepseek/deepseek-v4-flash",
        "name": "DeepSeek V4.1 Flash",
        "url": "https://routerai.ru/api/v1/chat/completions",
        "toolCalling": True,
        "vision": False,
        "maxInputTokens": 128000,
        "maxOutputTokens": 8192,
        "context_length": 128000,
        "tier": "cheap",
        "pricing_rub_per_1m": {"prompt": 50.0, "completion": 100.0},
        "supports_tools": True,
        "modality": "text_in_text_out",
        "supports_vision": False,
    }


@pytest.fixture
def endpoints_data() -> dict[str, list[dict[str, Any]]]:
    """Endpoint данные для нескольких моделей."""
    return {
        "deepseek/deepseek-v4-flash": [
            {
                "tag": "deepseek",
                "provider_name": "DeepSeek",
                "name": "DeepSeek V4.1 Flash",
                "status": 1,
                "context_length": 128000,
                "max_completion_tokens": 8192,
                "max_prompt_tokens": 128000,
                "supported_apis": ["chat"],
                "supported_parameters": ["tools", "streaming"],
                "rub_per_1m_prompt": 50.0,
                "rub_per_1m_completion": 100.0,
            },
            {
                "tag": "openai",
                "provider_name": "OpenAI",
                "name": "GPT-4o via OpenAI",
                "status": 1,
                "context_length": 128000,
                "max_completion_tokens": 4096,
                "max_prompt_tokens": 128000,
                "supported_apis": ["chat"],
                "supported_parameters": ["tools"],
                "rub_per_1m_prompt": 75.0,
                "rub_per_1m_completion": 150.0,
            },
        ],
        "openai/gpt-4o-mini": [
            {
                "tag": "openai",
                "provider_name": "OpenAI",
                "name": "GPT-4o Mini",
                "status": 1,
                "context_length": 128000,
                "max_completion_tokens": 16384,
                "max_prompt_tokens": 128000,
                "supported_apis": ["chat"],
                "supported_parameters": ["tools"],
                "rub_per_1m_prompt": 15.0,
                "rub_per_1m_completion": 60.0,
            },
        ],
        "missing-model/no-endpoints": [],
    }


@pytest.fixture
def non_token_endpoints() -> list[dict[str, Any]]:
    """Endpoint без tools — не является token endpoint."""
    return [
        {
            "tag": "example",
            "provider_name": "Example",
            "name": "Text Completion",
            "status": 1,
            "context_length": 8192,
            "max_completion_tokens": 2048,
            "max_prompt_tokens": 8192,
            "supported_apis": ["completion"],
            "supported_parameters": [],
            "rub_per_1m_prompt": 10.0,
            "rub_per_1m_completion": 20.0,
        },
    ]


# ---------------------------------------------------------------------------
# Тесты: _is_token_endpoint
# ---------------------------------------------------------------------------

class TestIsTokenEndpoint:
    """Проверка LOOSE правила: chat + tools + context_length."""

    def test_chat_tools_context(self, sample_entry):
        """Endpoint с chat, tools и context_length → True."""
        ep = {
            "supported_apis": ["chat"],
            "supported_parameters": ["tools", "streaming"],
            "context_length": 128000,
        }
        assert _is_token_endpoint(ep) is True

    def test_missing_tools(self):
        """Без tools → False."""
        ep = {
            "supported_apis": ["chat"],
            "supported_parameters": ["temperature"],
            "context_length": 128000,
        }
        assert _is_token_endpoint(ep) is False

    def test_missing_chat(self):
        """Без chat (только completion) → False."""
        ep = {
            "supported_apis": ["completion"],
            "supported_parameters": ["tools"],
            "context_length": 8192,
        }
        assert _is_token_endpoint(ep) is False

    def test_missing_context_length(self):
        """Без context_length → False."""
        ep = {
            "supported_apis": ["chat"],
            "supported_parameters": ["tools"],
            "context_length": None,
        }
        assert _is_token_endpoint(ep) is False

    def test_max_prompt_tokens_not_required(self):
        """max_prompt_tokens может быть None — LOOSE правило."""
        ep = {
            "supported_apis": ["chat"],
            "supported_parameters": ["tools"],
            "context_length": 128000,
            "max_prompt_tokens": None,
        }
        assert _is_token_endpoint(ep) is True


# ---------------------------------------------------------------------------
# Тесты: _cost_per_turn
# ---------------------------------------------------------------------------

class TestCostPerTurn:
    """Расчёт стоимости типового вызова."""

    def test_basic_calculation(self):
        """10k prompt tokens + 1k completion tokens при rub_per_1m."""
        ep = {
            "rub_per_1m_prompt": 50.0,
            "rub_per_1m_completion": 100.0,
        }
        cost = _cost_per_turn(ep)
        # 10000*50/1e6 + 1000*100/1e6 = 0.5 + 0.1 = 0.6
        assert cost == pytest.approx(0.6)

    def test_missing_pricing(self):
        """Отсутствие pricing → None."""
        ep = {"rub_per_1m_prompt": 50.0}
        assert _cost_per_turn(ep) is None

    def test_zero_pricing(self):
        """Нулевые цены → 0.0."""
        ep = {"rub_per_1m_prompt": 0.0, "rub_per_1m_completion": 0.0}
        assert _cost_per_turn(ep) == 0.0


# ---------------------------------------------------------------------------
# Тесты: _cheapest_endpoint
# ---------------------------------------------------------------------------

class TestCheapestEndpoint:
    """Выбор cheapest token endpoint."""

    def test_cheapest_among_multiple(self, endpoints_data):
        """Из двух endpoint'ов выбирает cheapest по cost_per_turn."""
        eps = endpoints_data["deepseek/deepseek-v4-flash"]
        cheapest = _cheapest_endpoint(eps)
        # deepseek: 50/100 vs openai: 75/150 → deepseek дешевле
        assert cheapest["tag"] == "deepseek"

    def test_single_endpoint(self, endpoints_data):
        """Один endpoint → возвращается он."""
        eps = endpoints_data["openai/gpt-4o-mini"]
        cheapest = _cheapest_endpoint(eps)
        assert cheapest["tag"] == "openai"

    def test_no_token_endpoints(self, non_token_endpoints):
        """Endpoint без tools → None."""
        assert _cheapest_endpoint(non_token_endpoints) is None

    def test_empty_list(self):
        """Пустой список → None."""
        assert _cheapest_endpoint([]) is None


# ---------------------------------------------------------------------------
# Тесты: normalize_model
# ---------------------------------------------------------------------------

class TestNormalizeModel:
    """normalize_model с endpoints_data → routes в output."""

    def test_without_endpoints_data_backward_compat(self, sample_entry):
        """Без endpoints_data → routes=[], cheapest=None (backward compat)."""
        result = normalize_model(sample_entry)
        assert result.get("routes") == []
        assert result.get("cheapest_cost_per_turn_estimate_rub") is None
        assert result.get("route_pricing_basis") is None

    def test_with_endpoints_data_routes_present(self, sample_entry, endpoints_data):
        """С endpoints_data → routes содержит delta_percent."""
        result = normalize_model(sample_entry, endpoints_data)
        routes = result.get("routes", [])
        assert len(routes) > 0
        # Каждая route имеет tag и delta_percent
        for route in routes:
            assert "tag" in route
            assert "delta_percent" in route

    def test_with_endpoints_data_cheapest_cost(self, sample_entry, endpoints_data):
        """С endpoints_data → cheapest_cost_per_turn_estimate_rub заполнено."""
        result = normalize_model(sample_entry, endpoints_data)
        cheapest = result.get("cheapest_cost_per_turn_estimate_rub")
        assert cheapest is not None
        assert isinstance(cheapest, float)
        # deepseek: 50/100 → cost_per_turn ≈ 0.6
        assert cheapest == pytest.approx(0.6, abs=0.01)

    def test_with_endpoints_data_pricing_basis(self, sample_entry, endpoints_data):
        """С endpoints_data → route_pricing_basis='route_min'."""
        result = normalize_model(sample_entry, endpoints_data)
        assert result.get("route_pricing_basis") == "route_min"

    def test_empty_endpoints_list(self, sample_entry, endpoints_data):
        """Модель с пустым списком endpoint'ов → routes=[]."""
        entry = dict(sample_entry, id="missing-model/no-endpoints")
        result = normalize_model(entry, endpoints_data)
        assert result.get("routes") == []
        assert result.get("cheapest_cost_per_turn_estimate_rub") is None

    def test_modality_and_vision_preserved(self, sample_entry, endpoints_data):
        """modality и supports_vision присутствуют на верхнем уровне normalize_model."""
        result = normalize_model(sample_entry, endpoints_data)
        assert result.get("modality") == "text_in_text_out"
        assert result.get("supports_vision") is False

    def test_routes_fields_present(self, sample_entry, endpoints_data):
        """routes, cheapest_cost_per_turn_estimate_rub, route_pricing_basis присутствуют."""
        result = normalize_model(sample_entry, endpoints_data)
        assert "routes" in result
        assert "cheapest_cost_per_turn_estimate_rub" in result
        assert "route_pricing_basis" in result


# ---------------------------------------------------------------------------
# Тесты: build_config_format
# ---------------------------------------------------------------------------

class TestBuildConfigFormat:
    """build_config_format → routes в каждой модели каталога."""

    def test_config_format_has_routes(self, sample_entry):
        """catalog.json entries содержат routes после fix."""
        # build_config_format принимает список entry-объектов + base_url
        entries = [dict(sample_entry, id="deepseek/deepseek-v4-flash")]
        endpoints_data = {
            "deepseek/deepseek-v4-flash": [
                {
                    "tag": "deepseek",
                    "provider_name": "DeepSeek",
                    "name": "DeepSeek V4.1 Flash",
                    "status": 1,
                    "context_length": 128000,
                    "max_completion_tokens": 8192,
                    "supported_apis": ["chat"],
                    "supported_parameters": ["tools", "streaming"],
                    "rub_per_1m_prompt": 50.0,
                    "rub_per_1m_completion": 100.0,
                },
            ],
        }
        result = build_config_format(entries, "https://routerai.ru/api/v1", endpoints_data)
        assert len(result) == 1
        model = result[0]
        assert model["id"] == "deepseek/deepseek-v4-flash"
        meta = model.get("_meta", {})
        assert "routes" in meta
        assert len(meta["routes"]) > 0

    def test_config_format_without_endpoints(self):
        """Без endpoints_data → routes=[] в _meta."""
        entries = [dict(id="test/model", name="Test Model")]
        result = build_config_format(entries, "https://example.com", None)
        assert len(result) == 1
        meta = result[0].get("_meta", {})
        assert meta.get("routes") == []


# ---------------------------------------------------------------------------
# Интеграционный тест: полный pipeline
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """End-to-end проверка: catalog → config_models → routes present."""

    def test_catalog_contains_routes_for_model_with_endpoints(self, sample_entry, endpoints_data, tmp_path):
        """Каталог с endpoints_data содержит routes для моделей с endpoint'ами."""
        catalog_path = tmp_path / "catalog.json"
        catalog = {"models": [sample_entry]}
        json.dump(catalog, open(catalog_path, "w", encoding="utf-8"))

        from build_report import normalize_model as nm  # noqa: E402
        result = nm(sample_entry, endpoints_data)

        assert "routes" in result
        assert len(result["routes"]) > 0
        assert "cheapest_cost_per_turn_estimate_rub" in result
        assert result["cheapest_cost_per_turn_estimate_rub"] is not None
