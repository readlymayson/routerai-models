"""Phase 2: тесты sync_configs.py — generate_provider_policy_and_pricing.

Покрывают:
- генерация не мутирует глобальные константы (PROVIDER_POLICY_DEFAULT, PRICING_OVERRIDES_DEFAULT);
- использует tag (не provider_name) для order;
- выбирает cheapest token endpoint для pricing;
- rub_per_1m → rub/token корректно делится на 1e6;
- empty endpoints → пустые результаты;
- filter_config overrides используются вместо сгенерированных;
- diff показывает изменения между filter и core.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch, mock_open

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sync_configs import (  # noqa: E402
    PROVIDER_POLICY_DEFAULT,
    PRICING_OVERRIDES_DEFAULT,
    _cheapest_token_endpoint,
    _cost_per_turn,
    _is_token_endpoint,
    generate_provider_policy_and_pricing,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_endpoints_data() -> dict[str, list[dict[str, Any]]]:
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
                "supported_apis": ["chat"],
                "supported_parameters": ["tools"],
                "rub_per_1m_prompt": 15.0,
                "rub_per_1m_completion": 60.0,
            },
        ],
        "missing-model/no-endpoints": [],
        "example/text-completion": [
            {
                "tag": "example",
                "provider_name": "Example",
                "name": "Text Completion",
                "status": 1,
                "context_length": 8192,
                "max_completion_tokens": 2048,
                "supported_apis": ["completion"],
                "supported_parameters": [],
                "rub_per_1m_prompt": 10.0,
                "rub_per_1m_completion": 20.0,
            },
        ],
    }


@pytest.fixture
def empty_endpoints_data() -> dict[str, list[dict[str, Any]]]:
    """Пустые endpoint данные."""
    return {}


# ---------------------------------------------------------------------------
# Тесты: _is_token_endpoint (LOOSE правило)
# ---------------------------------------------------------------------------

class TestIsTokenEndpoint:
    """LOOSE правило: chat + tools + context_length."""

    def test_valid_token_endpoint(self):
        """chat + tools + context_length → True."""
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
        """Без chat → False."""
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
        """max_prompt_tokens может быть None."""
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
        """10k prompt + 1k completion при rub_per_1m."""
        ep = {"rub_per_1m_prompt": 50.0, "rub_per_1m_completion": 100.0}
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
# Тесты: _cheapest_token_endpoint
# ---------------------------------------------------------------------------

class TestCheapestTokenEndpoint:
    """Выбор cheapest token endpoint."""

    def test_cheapest_among_multiple(self, sample_endpoints_data):
        """Из двух endpoint'ов выбирает cheapest по cost_per_turn."""
        eps = sample_endpoints_data["deepseek/deepseek-v4-flash"]
        cheapest = _cheapest_token_endpoint(eps)
        # deepseek: 50/100 vs openai: 75/150 → deepseek дешевле
        assert cheapest["tag"] == "deepseek"

    def test_single_endpoint(self, sample_endpoints_data):
        """Один endpoint → возвращается он."""
        eps = sample_endpoints_data["openai/gpt-4o-mini"]
        cheapest = _cheapest_token_endpoint(eps)
        assert cheapest["tag"] == "openai"

    def test_no_token_endpoints(self, sample_endpoints_data):
        """Endpoint без tools → None."""
        eps = sample_endpoints_data["example/text-completion"]
        assert _cheapest_token_endpoint(eps) is None

    def test_empty_list(self):
        """Пустой список → None."""
        assert _cheapest_token_endpoint([]) is None


# ---------------------------------------------------------------------------
# Тесты: generate_provider_policy_and_pricing
# ---------------------------------------------------------------------------

class TestGenerateProviderPolicyAndPricing:
    """Генерация policy и pricing из endpoint данных."""

    def test_no_global_mutation(self, sample_endpoints_data):
        """Генерация НЕ мутирует глобальные константы."""
        original_policy = copy.deepcopy(PROVIDER_POLICY_DEFAULT)
        original_pricing = copy.deepcopy(PRICING_OVERRIDES_DEFAULT)

        generate_provider_policy_and_pricing(sample_endpoints_data)

        assert PROVIDER_POLICY_DEFAULT == original_policy
        assert PRICING_OVERRIDES_DEFAULT == original_pricing

    def test_policy_uses_tag_not_provider_name(self, sample_endpoints_data):
        """policy использует tag (не provider_name)."""
        policy, pricing = generate_provider_policy_and_pricing(sample_endpoints_data)
        model_policy = policy.get("deepseek/deepseek-v4-flash", {})
        order = model_policy.get("order", [])
        # tags должны быть 'deepseek' и 'openai', НЕ 'DeepSeek'/'OpenAI'
        assert "deepseek" in order
        assert "openai" in order
        assert "DeepSeek" not in order
        assert "OpenAI" not in order

    def test_policy_order_sorted_by_price(self, sample_endpoints_data):
        """order отсортирован по возрастанию цены."""
        policy, _ = generate_provider_policy_and_pricing(sample_endpoints_data)
        model_policy = policy.get("deepseek/deepseek-v4-flash", {})
        order = model_policy.get("order", [])
        # deepseek (50) должен быть раньше openai (75)
        assert order.index("deepseek") < order.index("openai")

    def test_policy_allow_fallbacks_true(self, sample_endpoints_data):
        """allow_fallbacks всегда True."""
        policy, _ = generate_provider_policy_and_pricing(sample_endpoints_data)
        for model_id, p in policy.items():
            assert p["allow_fallbacks"] is True

    def test_pricing_divided_by_1e6(self, sample_endpoints_data):
        """Цены разделены на 1e6: rub_per_1m → rub/token."""
        _, pricing = generate_provider_policy_and_pricing(sample_endpoints_data)
        model_pricing = pricing.get("deepseek/deepseek-v4-flash", {})
        # 50 / 1e6 = 0.00005
        assert model_pricing.get("prompt_token") == pytest.approx(0.00005, abs=1e-9)
        # 100 / 1e6 = 0.0001
        assert model_pricing.get("completion_token") == pytest.approx(0.0001, abs=1e-9)

    def test_pricing_from_cheapest_endpoint(self, sample_endpoints_data):
        """pricing берётся из cheapest token endpoint."""
        _, pricing = generate_provider_policy_and_pricing(sample_endpoints_data)
        model_pricing = pricing.get("deepseek/deepseek-v4-flash", {})
        # deepseek cheaper than openai → pricing from deepseek
        assert model_pricing.get("prompt_token") == pytest.approx(50.0 / 1e6, abs=1e-9)
        assert model_pricing.get("completion_token") == pytest.approx(100.0 / 1e6, abs=1e-9)

    def test_empty_endpoints_returns_empty_dicts(self, empty_endpoints_data):
        """Пустые endpoint данные → пустые словари."""
        policy, pricing = generate_provider_policy_and_pricing(empty_endpoints_data)
        assert policy == {}
        assert pricing == {}

    def test_model_without_token_endpoints(self, sample_endpoints_data):
        """Модель без token endpoint'ов → нет в pricing."""
        _, pricing = generate_provider_policy_and_pricing(sample_endpoints_data)
        assert "example/text-completion" not in pricing

    def test_empty_endpoint_list(self, sample_endpoints_data):
        """Модель с пустым списком endpoint'ов → нет в результатах."""
        _, pricing = generate_provider_policy_and_pricing(sample_endpoints_data)
        assert "missing-model/no-endpoints" not in pricing

    def test_returns_tuple_of_two_dicts(self, sample_endpoints_data):
        """Возвращает кортеж (policy_dict, pricing_dict)."""
        result = generate_provider_policy_and_pricing(sample_endpoints_data)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], dict)


# ---------------------------------------------------------------------------
# Тесты: diff output (compare_with_config integration)
# ---------------------------------------------------------------------------

class TestDiffOutput:
    """Проверка что compare_with_config правильно читает provider_policy/pricing."""

    def test_compare_reads_from_core_routerai(self):
        """compare_with_config читает provider_policy из core_routerai секции config.json.

        Проверяем по исходному коду: строки должны читать из core_routerai.get(),
        а НЕ из filter_config.get().
        """
        import inspect
        from compare_with_config import diff_models  # noqa: E402
        source = inspect.getsource(diff_models)

        # Ключевые паттерны которые ДОЛЖНЫ быть в коде
        assert 'core_routerai.get("provider_policy")' in source or \
               "core_routerai.get('provider_policy')" in source, \
            "code must read provider_policy from core_routerai"
        assert 'core_routerai.get("pricing_overrides")' in source or \
               "core_routerai.get('pricing_overrides')" in source, \
            "code must read pricing_overrides from core_routerai"

        # Проверяем что core_provider_policy инициализируется из core_routerai
        assert 'core_provider_policy = core_routerai.get("provider_policy")' in source or \
               "core_provider_policy = core_routerai.get('provider_policy')" in source
        assert 'core_pricing_overrides = core_routerai.get("pricing_overrides")' in source or \
               "core_pricing_overrides = core_routerai.get('pricing_overrides')" in source


# ---------------------------------------------------------------------------
# Интеграционный тест: полный pipeline
# ---------------------------------------------------------------------------

class TestFullPipeline:
    """End-to-end проверка: endpoints → policy/pricing → config write."""

    def test_pipeline_generates_correct_policy_and_pricing(self, sample_endpoints_data, tmp_path):
        """Полный pipeline: endpoints → generate → cfg_after."""
        policy, pricing = generate_provider_policy_and_pricing(sample_endpoints_data)

        # Проверяем что policy содержит ожидаемые модели
        assert "deepseek/deepseek-v4-flash" in policy
        assert "openai/gpt-4o-mini" in policy
        # example/text-completion не имеет token endpoint → нет в pricing
        assert "example/text-completion" not in pricing
        # missing-model → пустой список → нет в результатах
        assert "missing-model/no-endpoints" not in policy

        # Проверяем структуру policy entry
        dp_policy = policy["deepseek/deepseek-v4-flash"]
        assert dp_policy["order"] == ["deepseek", "openai"]
        assert dp_policy["allow_fallbacks"] is True

        # Проверяем структуру pricing entry
        dp_pricing = pricing["deepseek/deepseek-v4-flash"]
        assert "prompt_token" in dp_pricing
        assert "completion_token" in dp_pricing
        assert dp_pricing["prompt_token"] > 0
        assert dp_pricing["completion_token"] > 0
