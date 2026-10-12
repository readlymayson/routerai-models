#!/usr/bin/env python3
"""Endpoint catalog and route pricing for routerai.ru.

Выгружает и парсит данные об endpoint'ах для каждой модели:
GET {base_url}/models/{model_id}/endpoints

Записывает data/endpoints.json (атомарно: tmp + replace).

Использование:
    python3 endpoints.py

Авторизация для endpoint'ов НЕ требуется (по документации RouterAI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Ошибка: не установлен модуль httpx. Выполните: pip install -r requirements.txt\n"
    )
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Типы
# --------------------------------------------------------------------------- #
class Endpoint(TypedDict):
    """Нормализованная запись endpoint'а модели."""

    tag: str
    provider_name: str
    name: str
    status: int
    quantization: Optional[str]
    countries: Optional[list[str]]
    context_length: Optional[int]
    max_completion_tokens: Optional[int]
    max_prompt_tokens: Optional[int]
    supported_apis: list[str]
    supported_parameters: list[str]
    pricing: dict[str, float]
    pricing_units: dict[str, str]
    variable_pricings: list[dict[str, Any]]
    # Вычисленные поля
    rub_per_1m_prompt: Optional[float]
    rub_per_1m_completion: Optional[float]
    rub_per_1m_input_cache_read: Optional[float]
    rub_per_1m_image_output: Optional[float]
    supports_tools: bool


# --------------------------------------------------------------------------- #
# Парсеры
# --------------------------------------------------------------------------- #
def _is_token_unit(unit: Optional[str]) -> bool:
    """True если единица тарификации — токен."""
    return unit == "token"


def _convert_rub_per_1m(price: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Конвертация цены в ₽/1M: умножаем на 1e6 ТОЛЬКО для token-единиц.

    Для других единиц (request, image, megapixel, second, search_unit)
    цена остаётся в исходной единице (не умножается).
    """
    if price is None:
        return None
    if _is_token_unit(unit):
        return round(price * 1_000_000.0, 6)
    return price


def parse_endpoint(raw: dict[str, Any]) -> Endpoint:
    """Нормализует одну запись endpoint'а из API-ответа."""
    pricing = raw.get("pricing") or {}
    pricing_units = raw.get("pricing_units") or {}
    supported_params = raw.get("supported_parameters") or []
    supported_apis = raw.get("supported_apis") or []

    countries_raw = raw.get("countries") or raw.get("country")
    if isinstance(countries_raw, str):
        countries: Optional[list[str]] = [countries_raw]
    elif isinstance(countries_raw, list):
        countries = countries_raw
    else:
        countries = None

    return {
        "tag": raw.get("tag", ""),
        "provider_name": raw.get("provider_name", ""),
        "name": raw.get("name", ""),
        "status": raw.get("status", 0),
        "quantization": raw.get("quantization"),
        "countries": countries,
        "context_length": raw.get("context_length"),
        "max_completion_tokens": raw.get("max_completion_tokens"),
        "max_prompt_tokens": raw.get("max_prompt_tokens"),
        "supported_apis": supported_apis,
        "supported_parameters": supported_params,
        "pricing": {k: float(v) for k, v in pricing.items() if isinstance(v, (int, float))},
        "pricing_units": {k: str(v) for k, v in pricing_units.items()},
        "variable_pricings": raw.get("variable_pricings") or [],
        # Вычисленные поля — конвертация единиц
        "rub_per_1m_prompt": _convert_rub_per_1m(
            pricing.get("prompt"), pricing_units.get("prompt")
        ),
        "rub_per_1m_completion": _convert_rub_per_1m(
            pricing.get("completion"), pricing_units.get("completion")
        ),
        "rub_per_1m_input_cache_read": _convert_rub_per_1m(
            pricing.get("input_cache_read"), pricing_units.get("input_cache_read")
        ),
        "rub_per_1m_image_output": _convert_rub_per_1m(
            pricing.get("image_output"), pricing_units.get("image_output")
        ),
        "supports_tools": "tools" in supported_params,
    }


def parse_endpoints(payload: dict[str, Any]) -> list[Endpoint]:
    """Извлекает и нормализует список endpoint'ов из API-ответа.

    Принимает:
      - {"data": {"endpoints": [...]}} — полный ответ API
      - {"data": [...]} — список endpoint'ов напрямую в data
      - {"endpoints": [...]} — список endpoint'ов напрямую
      - [...] — список endpoint'ов

    Возвращает пустой список, если endpoint'ов не найдено.
    """
    if isinstance(payload, list):
        raw_endpoints = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            raw_endpoints = data.get("endpoints", [])
        elif isinstance(data, list):
            raw_endpoints = data
        else:
            raw_endpoints = payload.get("endpoints", [])
    else:
        return []

    if not isinstance(raw_endpoints, list):
        return []

    return [parse_endpoint(e) for e in raw_endpoints if isinstance(e, dict)]


# --------------------------------------------------------------------------- #
# Вычисляемые функции
# --------------------------------------------------------------------------- #
def endpoint_rub_per_1m(ep: Endpoint) -> dict[str, Optional[float]]:
    """Возвращает rub_per_1m для всех ценовых ключей endpoint'а.

    Конвертация: rub_per_1m[key] = pricing[key] × 1e6 ТОЛЬКО если
    pricing_units[key] == "token". Для других единиц цена остаётся
    в исходной единице (не участвует в токенных сравнениях).
    """
    pricing = ep.get("pricing", {})
    units = ep.get("pricing_units", {})
    result: dict[str, Optional[float]] = {}
    for key in ("prompt", "completion", "input_cache_read", "image_output", "seconds"):
        unit = units.get(key)
        price = pricing.get(key)
        result[f"rub_per_1m_{key}"] = _convert_rub_per_1m(price, unit)
    return result


def cost_per_turn(
    ep: Endpoint,
    prompt_tokens: int = 10_000,
    completion_tokens: int = 1_000,
) -> Optional[float]:
    """Стоимость типового вызова (₽) для endpoint'а.

    Формула: C = (Np × Pp + Nc × Pc) / 1e6
    где Np/Nc — кол-во входных/выходных токенов из call_params,
    Pp/Pc — rub_per_1m_prompt / rub_per_1m_completion (₽ за 1M).

    Возвращает None если недоступны цены для расчёта.
    """
    pp = ep.get("rub_per_1m_prompt")
    pc = ep.get("rub_per_1m_completion")
    if pp is None or pc is None:
        return None
    cost = (prompt_tokens * pp + completion_tokens * pc) / 1_000_000.0
    return round(cost, 6)


def is_token_endpoint(ep: Endpoint) -> bool:
    """True если endpoint подходит для агентских вызовов:
    chat в supported_apis, tools в supported_parameters,
    достаточные лимиты (context_length > 0, max_prompt_tokens > 0).
    """
    if "chat" not in ep.get("supported_apis", []):
        return False
    if not ep.get("supports_tools"):
        return False
    if not ep.get("context_length"):
        return False
    if not ep.get("max_prompt_tokens"):
        return False
    return True


def endpoints_with_tools(eps: list[Endpoint]) -> list[Endpoint]:
    """Фильтрует endpoint'ы, поддерживающие tools."""
    return [ep for ep in eps if ep.get("supports_tools")]


def cheapest_endpoint(eps: list[Endpoint]) -> Optional[Endpoint]:
    """Endpoint с минимальной стоимостью вызова (cost_per_turn)
    среди endpoint'ов с chat + tools.

    Возвращает None если подходящих endpoint'ов нет.
    """
    token_eps = [ep for ep in eps if is_token_endpoint(ep)]
    if not token_eps:
        return None

    def _cost(ep: Endpoint) -> float:
        c = cost_per_turn(ep)
        return c if c is not None else float("inf")

    return min(token_eps, key=_cost)


def route_vs_aggregate(
    aggregate_cost_per_turn: float,
    eps: list[Endpoint],
) -> dict[str, Any]:
    """Сравнение route-цен с агрегатной ценой.

    Возвращает:
      - aggregate_cost_per_turn: агрегатная цена (₽/вызов)
      - routes: список {tag, cost_per_turn, delta_percent} для каждого endpoint'а

    delta_percent = (route - aggregate) / aggregate × 100
    """
    routes: list[dict[str, Any]] = []
    for ep in eps:
        c = cost_per_turn(ep)
        if c is None:
            continue
        if aggregate_cost_per_turn > 0:
            delta = (c - aggregate_cost_per_turn) / aggregate_cost_per_turn * 100.0
        else:
            delta = 0.0
        routes.append({
            "tag": ep.get("tag", ""),
            "cost_per_turn": c,
            "delta_percent": round(delta, 2),
        })
    routes.sort(key=lambda r: r["cost_per_turn"])
    return {
        "aggregate_cost_per_turn": aggregate_cost_per_turn,
        "routes": routes,
    }


# --------------------------------------------------------------------------- #
# Сетевой слой
# --------------------------------------------------------------------------- #
def fetch_endpoints(
    model_ids: list[str],
    cfg: dict[str, Any],
    api_key: Optional[str] = None,
) -> dict[str, list[Endpoint]]:
    """Выгружает endpoint'ы для списка моделей.

    Args:
        model_ids: список id моделей (вида "provider/model").
        cfg: конфиг из filter_config.json (секции api, endpoints, paths).
        api_key: Bearer-токен (не используется для endpoint'ов, но
            пробрасывается для совместимости).

    Returns:
        Словарь {model_id: [Endpoint, ...]}. Модели без endpoint'ов
        отсутствуют в словаре (не с пустым списком).

    Записывает data/endpoints.json (атомарно).
    """
    api_cfg = cfg.get("api", {})
    base_url = os.environ.get("ROUTERAI_API_URL", api_cfg.get("base_url", "https://routerai.ru/api/v1"))
    timeout = float(os.environ.get("ROUTERAI_TIMEOUT", api_cfg.get("timeout_seconds", 30)))
    user_agent = api_cfg.get("user_agent", "routerai-models-catalog/1.0")

    endpoints_path = PROJECT_ROOT / "data" / "endpoints.json"
    result: dict[str, list[Endpoint]] = {}

    for model_id in model_ids:
        url = f"{base_url.rstrip('/')}/models/{model_id}/endpoints"
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
                eps = parse_endpoints(payload)
                if eps:
                    result[model_id] = eps
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"[endpoints] Ошибка для {model_id}: {exc}\n")

    # Атомарная запись
    endpoints_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = endpoints_path.with_suffix(endpoints_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    tmp_path.replace(endpoints_path)

    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _resolve_model_ids(cfg: dict[str, Any]) -> list[str]:
    """Разрешает список model_ids из scope filter_config.endpoints.scope."""
    scope = cfg.get("endpoints", {}).get("scope", ["selected", "reference", "asr"])
    if not scope:
        scope = ["selected", "reference", "asr"]

    ids: set[str] = set()

    if "selected" in scope:
        rec_path = PROJECT_ROOT / cfg.get("paths", {}).get("recommendations", "data/recommendations.json")
        if rec_path.exists():
            try:
                rec = json.loads(rec_path.read_text(encoding="utf-8"))
                for cat in rec.get("categories", {}).values():
                    for m in cat.get("top", []):
                        mid = m.get("id")
                        if mid:
                            ids.add(mid)
            except (json.JSONDecodeError, OSError):
                pass

    if "reference" in scope:
        ref_id = (cfg.get("reference_model") or {}).get("id")
        if ref_id:
            ids.add(ref_id)

    if "asr" in scope:
        asr_cat = PROJECT_ROOT / cfg.get("paths", {}).get("asr_catalog", "data/asr_catalog.json")
        if asr_cat.exists():
            try:
                asr_data = json.loads(asr_cat.read_text(encoding="utf-8"))
                models = asr_data.get("models", []) if isinstance(asr_data, dict) else asr_data
                for m in models:
                    mid = m.get("id")
                    if mid:
                        ids.add(mid)
            except (json.JSONDecodeError, OSError):
                pass

    return sorted(ids)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "filter_config.json",
        help="Путь к filter_config.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать кол-во endpoint'ов, не сохранять",
    )
    args = parser.parse_args(argv)

    try:
        cfg = json.loads(args.config.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Ошибка: не найден или повреждён конфиг {args.config}: {exc}\n")
        return 1

    model_ids = _resolve_model_ids(cfg)
    if not model_ids:
        print("[endpoints] Нет моделей для выгрузки endpoint'ов")
        return 0

    api_key = os.environ.get("ROUTERAI_API_KEY")

    if args.dry_run:
        print(f"[endpoints] dry-run: {len(model_ids)} моделей, endpoint'ы не выгружаются")
        return 0

    print(f"[endpoints] Выгрузка endpoint'ов для {len(model_ids)} моделей")
    try:
        result = fetch_endpoints(model_ids, cfg, api_key)
    except Exception as exc:
        sys.stderr.write(f"[endpoints] Критическая ошибка: {exc}\n")
        # Записать пустой результат — конвейер не должен падать
        result = {}

    total = sum(len(v) for v in result.values())
    print(f"[endpoints] Сохранено: data/endpoints.json ({len(result)} моделей, {total} endpoint'ов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
