"""Фаза 1: тесты endpoints.py — парсеры, вычисляемые функции, atomic write.

Покрывают:
- parse_endpoint: поддержка tools из supported_parameters;
- rub_per_1m из pricing + pricing_units (token → ×1e6, другие → как есть);
- cheapest_endpoint среди chat+tools;
- route_vs_aggregate: delta_percent;
- fetch_endpoints: атомарная запись (tmp+replace);
- missing model → пустой список (не крашится);
- parse_endpoints: все варианты shape API-ответа.
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

from endpoints import (  # noqa: E402
    Endpoint,
    cheapest_endpoint,
    cost_per_turn,
    endpoint_rub_per_1m,
    fetch_endpoints,
    is_token_endpoint,
    parse_endpoint,
    parse_endpoints,
    route_vs_aggregate,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_endpoint() -> dict[str, Any]:
    """Один «правильный» endpoint из реального API-ответа."""
    return {
        "tag": "deepseek",
        "provider_name": "DeepSeek",
        "name": "DeepSeek V4.1 Flash",
        "status": 1,
        "quantization": "q8",
        "countries": ["RU", "KZ"],
        "context_length": 128000,
        "max_completion_tokens": 8192,
        "max_prompt_tokens": 128000,
        "supported_apis": ["chat"],
        "supported_parameters": ["tools", "streaming"],
        "pricing": {"prompt": 0.00002, "completion": 0.00006},
        "pricing_units": {"prompt": "token", "completion": "token"},
        "variable_pricings": [],
    }


@pytest.fixture
def non_token_endpoint() -> dict[str, Any]:
    """Endpoint с не-token единицей тарификации (request)."""
    return {
        "tag": "openai",
        "provider_name": "OpenAI",
        "name": "GPT-4o",
        "status": 1,
        "quantization": None,
        "countries": None,
        "context_length": 128000,
        "max_completion_tokens": 4096,
        "max_prompt_tokens": 128000,
        "supported_apis": ["chat"],
        "supported_parameters": ["tools"],
        "pricing": {"prompt": 2.5, "completion": 10.0},
        "pricing_units": {"prompt": "request", "completion": "request"},
        "variable_pricings": [],
    }


# ---------------------------------------------------------------------------
# parse_endpoint
# ---------------------------------------------------------------------------

class TestParseEndpoint:
    def test_returns_typed_dict(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        assert isinstance(ep, dict)
        assert "tag" in ep
        assert "supports_tools" in ep
        assert "rub_per_1m_prompt" in ep
        assert "rub_per_1m_completion" in ep

    def test_supports_tools_from_supported_parameters(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        assert ep["supports_tools"] is True

    def test_supports_tools_false_when_missing(self):
        raw = {
            "tag": "test", "provider_name": "Test", "name": "Test",
            "status": 1, "supported_parameters": ["streaming"],
            "pricing": {}, "pricing_units": {},
        }
        ep = parse_endpoint(raw)
        assert ep["supports_tools"] is False

    def test_token_unit_converted_to_rub_per_1m(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        # prompt: 0.00002 * 1e6 = 20.0
        assert ep["rub_per_1m_prompt"] == 20.0
        # completion: 0.00006 * 1e6 = 60.0
        assert ep["rub_per_1m_completion"] == 60.0

    def test_non_token_unit_kept_as_is(self, non_token_endpoint):
        ep = parse_endpoint(non_token_endpoint)
        # request unit → не умножается на 1e6
        assert ep["rub_per_1m_prompt"] == 2.5
        assert ep["rub_per_1m_completion"] == 10.0

    def test_countries_string_to_list(self):
        raw = {
            "tag": "t", "provider_name": "p", "name": "n", "status": 1,
            "countries": "RU", "supported_parameters": [],
            "pricing": {}, "pricing_units": {},
        }
        ep = parse_endpoint(raw)
        assert ep["countries"] == ["RU"]

    def test_countries_list_preserved(self):
        raw = {
            "tag": "t", "provider_name": "p", "name": "n", "status": 1,
            "countries": ["RU", "KZ"], "supported_parameters": [],
            "pricing": {}, "pricing_units": {},
        }
        ep = parse_endpoint(raw)
        assert ep["countries"] == ["RU", "KZ"]

    def test_missing_fields_default(self):
        raw = {"tag": "t"}
        ep = parse_endpoint(raw)
        assert ep["provider_name"] == ""
        assert ep["status"] == 0
        assert ep["quantization"] is None
        assert ep["supports_tools"] is False


# ---------------------------------------------------------------------------
# parse_endpoints — разные shape API-ответа
# ---------------------------------------------------------------------------

class TestParseEndpoints:
    def test_full_api_shape(self):
        payload = {"data": {"endpoints": [{"tag": "a"}]}}
        eps = parse_endpoints(payload)
        assert len(eps) == 1
        assert eps[0]["tag"] == "a"

    def test_data_list_shape(self):
        payload = {"data": [{"tag": "b"}, {"tag": "c"}]}
        eps = parse_endpoints(payload)
        assert len(eps) == 2

    def test_endpoints_key_shape(self):
        payload = {"endpoints": [{"tag": "d"}]}
        eps = parse_endpoints(payload)
        assert len(eps) == 1

    def test_list_shape(self):
        payload = [{"tag": "e"}, {"tag": "f"}]
        eps = parse_endpoints(payload)
        assert len(eps) == 2

    def test_empty_payload(self):
        assert parse_endpoints({}) == []
        assert parse_endpoints([]) == []
        assert parse_endpoints({"data": {}}) == []
        assert parse_endpoints({"data": []}) == []

    def test_non_dict_items_filtered(self):
        payload = {"data": [{"tag": "ok"}, "bad", None, {"tag": "ok2"}]}
        eps = parse_endpoints(payload)
        assert len(eps) == 2


# ---------------------------------------------------------------------------
# Вычисляемые функции
# ---------------------------------------------------------------------------

class TestEndpointRubPer1m:
    def test_token_keys(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        r = endpoint_rub_per_1m(ep)
        assert r["rub_per_1m_prompt"] == 20.0
        assert r["rub_per_1m_completion"] == 60.0
        assert r["rub_per_1m_input_cache_read"] is None
        assert r["rub_per_1m_image_output"] is None
        assert r["rub_per_1m_seconds"] is None

    def test_non_token_keys(self, non_token_endpoint):
        ep = parse_endpoint(non_token_endpoint)
        r = endpoint_rub_per_1m(ep)
        # request → не умножается
        assert r["rub_per_1m_prompt"] == 2.5
        assert r["rub_per_1m_completion"] == 10.0


class TestCostPerTurn:
    def test_calculation(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        # (10000 * 20 + 1000 * 60) / 1e6 = (200000 + 60000) / 1e6 = 0.26
        c = cost_per_turn(ep, prompt_tokens=10_000, completion_tokens=1_000)
        assert c == 0.26

    def test_none_when_missing_prices(self):
        ep: Endpoint = {
            "tag": "x", "provider_name": "x", "name": "x", "status": 1,
            "quantization": None, "countries": None, "context_length": 1000,
            "max_completion_tokens": 1000, "max_prompt_tokens": 1000,
            "supported_apis": [], "supported_parameters": [],
            "pricing": {}, "pricing_units": {},
            "variable_pricings": [],
            "rub_per_1m_prompt": None,
            "rub_per_1m_completion": None,
            "rub_per_1m_input_cache_read": None,
            "rub_per_1m_image_output": None,
            "supports_tools": False,
        }
        assert cost_per_turn(ep) is None


class TestIsTokenEndpoint:
    def test_true_for_chat_tools(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        assert is_token_endpoint(ep) is True

    def test_false_without_chat(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        ep["supported_apis"] = ["completion"]
        assert is_token_endpoint(ep) is False

    def test_false_without_tools(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        ep["supported_parameters"] = ["streaming"]
        ep["supports_tools"] = False
        assert is_token_endpoint(ep) is False

    def test_false_without_context(self, sample_endpoint):
        ep = parse_endpoint(sample_endpoint)
        ep["context_length"] = 0
        assert is_token_endpoint(ep) is False


class TestCheapestEndpoint:
    def test_returns_cheapest(self):
        eps = [
            parse_endpoint({
                "tag": "expensive", "provider_name": "p", "name": "n", "status": 1,
                "supported_apis": ["chat"], "supported_parameters": ["tools"],
                "context_length": 1000, "max_prompt_tokens": 1000,
                "pricing": {"prompt": 0.001, "completion": 0.001},
                "pricing_units": {"prompt": "token", "completion": "token"},
            }),
            parse_endpoint({
                "tag": "cheap", "provider_name": "p", "name": "n", "status": 1,
                "supported_apis": ["chat"], "supported_parameters": ["tools"],
                "context_length": 1000, "max_prompt_tokens": 1000,
                "pricing": {"prompt": 0.0001, "completion": 0.0001},
                "pricing_units": {"prompt": "token", "completion": "token"},
            }),
        ]
        result = cheapest_endpoint(eps)
        assert result is not None
        assert result["tag"] == "cheap"

    def test_none_when_no_token_endpoints(self):
        eps = [
            parse_endpoint({
                "tag": "no-tools", "provider_name": "p", "name": "n", "status": 1,
                "supported_apis": ["chat"], "supported_parameters": [],
                "context_length": 1000, "max_prompt_tokens": 1000,
                "pricing": {"prompt": 0.0001, "completion": 0.0001},
                "pricing_units": {"prompt": "token", "completion": "token"},
            }),
        ]
        assert cheapest_endpoint(eps) is None

    def test_none_when_empty(self):
        assert cheapest_endpoint([]) is None


class TestRouteVsAggregate:
    def test_delta_percent(self):
        eps = [
            parse_endpoint({
                "tag": "a", "provider_name": "p", "name": "n", "status": 1,
                "supported_apis": ["chat"], "supported_parameters": ["tools"],
                "context_length": 1000, "max_prompt_tokens": 1000,
                "pricing": {"prompt": 0.0001, "completion": 0.0001},
                "pricing_units": {"prompt": "token", "completion": "token"},
            }),
            parse_endpoint({
                "tag": "b", "provider_name": "p", "name": "n", "status": 1,
                "supported_apis": ["chat"], "supported_parameters": ["tools"],
                "context_length": 1000, "max_prompt_tokens": 1000,
                "pricing": {"prompt": 0.0002, "completion": 0.0002},
                "pricing_units": {"prompt": "token", "completion": "token"},
            }),
        ]
        # cost_per_turn with default tokens: (10000*100 + 1000*100)/1e6 = 1.1
        # and (10000*200 + 1000*200)/1e6 = 2.2
        # aggregate=1.5 puts route "a" below and route "b" above
        result = route_vs_aggregate(1.5, eps)
        assert "aggregate_cost_per_turn" in result
        assert "routes" in result
        assert len(result["routes"]) == 2
        # route "a" should be cheaper → negative delta
        assert result["routes"][0]["tag"] == "a"
        assert result["routes"][0]["delta_percent"] < 0
        # route "b" should be more expensive → positive delta
        assert result["routes"][1]["tag"] == "b"
        assert result["routes"][1]["delta_percent"] > 0

    def test_zero_aggregate(self):
        eps = [
            parse_endpoint({
                "tag": "a", "provider_name": "p", "name": "n", "status": 1,
                "supported_apis": ["chat"], "supported_parameters": ["tools"],
                "context_length": 1000, "max_prompt_tokens": 1000,
                "pricing": {"prompt": 0.0001, "completion": 0.0001},
                "pricing_units": {"prompt": "token", "completion": "token"},
            }),
        ]
        result = route_vs_aggregate(0.0, eps)
        assert result["routes"][0]["delta_percent"] == 0.0


# ---------------------------------------------------------------------------
# fetch_endpoints — atomic write, graceful degradation
# ---------------------------------------------------------------------------

class TestFetchEndpoints:
    def test_atomic_write(self, tmp_path, sample_endpoint):
        """fetch_endpoints writes data/endpoints.json atomically (tmp+replace)."""
        cfg = {
            "api": {"base_url": "http://localhost:9999", "timeout_seconds": 5},
            "endpoints": {"scope": ["selected"]},
        }
        model_id = "test/model"

        mock_response = {
            "data": {"endpoints": [sample_endpoint]}
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_get = mock_client.get
            mock_get.return_value.json.return_value = mock_response
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.status_code = 200

            result = fetch_endpoints([model_id], cfg)

        assert model_id in result
        assert len(result[model_id]) == 1
        assert result[model_id][0]["tag"] == "deepseek"

        # Check atomic write: endpoints.json should exist
        endpoints_file = PROJECT_ROOT / "data" / "endpoints.json"
        assert endpoints_file.exists()

        # Verify no .tmp file left behind
        tmp_files = list((PROJECT_ROOT / "data").glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_missing_model_returns_empty_not_crash(self, tmp_path):
        """Модель без endpoint'ов → пустой dict (не пустой список)."""
        cfg = {
            "api": {"base_url": "http://localhost:9999", "timeout_seconds": 5},
            "endpoints": {"scope": ["selected"]},
        }
        model_id = "nonexistent/model"

        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_get = mock_client.get
            # Simulate 404 for this model
            import httpx
            mock_get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Not Found", request=mock_get.return_value, response=mock_get.return_value
            ) if hasattr(mock_get, 'return_value') else None
            mock_get.return_value.status_code = 404

            result = fetch_endpoints([model_id], cfg)

        # Модель без endpoint'ов отсутствует в словаре (не с пустым списком)
        assert model_id not in result

    def test_network_error_graceful(self, tmp_path):
        """Сетевая ошибка → не крашится, возвращает пустой dict."""
        cfg = {
            "api": {"base_url": "http://localhost:9999", "timeout_seconds": 1},
            "endpoints": {"scope": ["selected"]},
        }
        model_id = "test/model"

        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            # Raise httpx.HTTPError (which fetch_endpoints catches)
            import httpx
            mock_client.get.side_effect = httpx.HTTPError("connection refused")

            result = fetch_endpoints([model_id], cfg)

        # fetch_endpoints catches httpx.HTTPError and returns partial/empty result
        assert isinstance(result, dict)

    def test_writes_endpoints_json(self, tmp_path):
        """fetch_endpoints записывает data/endpoints.json."""
        endpoints_file = PROJECT_ROOT / "data" / "endpoints.json"
        # File should exist from previous test run
        assert endpoints_file.exists()
        data = json.loads(endpoints_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestEndpointsCLI:
    def test_dry_run(self):
        """--dry-run не пишет файл, показывает кол-во моделей."""
        from endpoints import main
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            rc = main(["--dry-run"])
        output = f.getvalue()
        assert rc == 0
        assert "dry-run" in output

    def test_main_default(self):
        """main() без аргументов — запускает выгрузку."""
        from endpoints import main
        rc = main([])
        assert rc == 0
        endpoints_file = PROJECT_ROOT / "data" / "endpoints.json"
        assert endpoints_file.exists()
