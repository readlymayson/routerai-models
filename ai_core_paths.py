#!/usr/bin/env python3
"""Общие утилиты routerai-models: пути ai-core, защита записи конфигов.

Единый источник истины для расположения ai-core и связанных файлов,
чтобы скрипты (sync_configs / compare_with_config / recommend) не держали
захардкоженные абсолютные пути в нескольких местах.

Настройки — в filter_config.json:

    "ai_core": {
        "project_root": "/home/dev/projects/ai-core",
        "config_path": "config/config.json",   # относительно project_root
        "stale_after_hours": 72                # возраст каталога, после
                                               # которого запись запрещена
    },

Пути можно переопределить через env:
    AI_CORE_ROOT       — корень проекта ai-core
    AI_CORE_CONFIG     — полный путь к config.json ai-core
    ROUTERAI_CATALOG   — полный путь к data/catalog.json
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_AI_CORE_ROOT = Path("/home/dev/projects/ai-core")
DEFAULT_CONFIG_PATH = "config/config.json"
DEFAULT_STALE_HOURS = 72.0

# Потолки контекста для конфигураций (maxInputTokens/maxOutputTokens).
# Базовая оценка: 80% контекста на вход / остаток на выход, затем лимит
# по тиру (token_limits.max_input_tokens / max_output_tokens в
# filter_config.json). Используются как жёсткие потолки: 250K/150K/100K вход
# и 16K/8K/8K выход для cheap/medium/premium.
DEFAULT_CONTEXT_LENGTH = 128_000
DEFAULT_TOKEN_LIMITS = {
    "max_input_tokens": {"cheap": 250_000, "medium": 150_000, "premium": 100_000},
    "max_output_tokens": {"cheap": 16_000, "medium": 8_000, "premium": 8_000},
}


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_filter_config() -> dict[str, Any]:
    """Загружает filter_config.json (кэшируется)."""
    if not hasattr(load_filter_config, "_cache"):
        load_filter_config._cache = load_json(PROJECT_ROOT / "filter_config.json")
    return load_filter_config._cache


def ai_core_root() -> Path:
    """Корень проекта ai-core (env AI_CORE_ROOT > filter_config.json > дефолт)."""
    env_root = os.environ.get("AI_CORE_ROOT")
    if env_root:
        return Path(env_root)
    try:
        cfg = load_filter_config()
        root = cfg.get("ai_core", {}).get("project_root")
        if root:
            return Path(root)
    except Exception:
        pass
    return DEFAULT_AI_CORE_ROOT


def ai_core_config_path() -> Path:
    """Полный путь к config.json ai-core (env AI_CORE_CONFIG приоритетнее)."""
    env_cfg = os.environ.get("AI_CORE_CONFIG")
    if env_cfg:
        return Path(env_cfg)
    try:
        cfg = load_filter_config()
        rel = cfg.get("ai_core", {}).get("config_path", DEFAULT_CONFIG_PATH)
    except Exception:
        rel = DEFAULT_CONFIG_PATH
    return ai_core_root() / rel


def catalog_path() -> Path:
    """Полный путь к data/catalog.json (env ROUTERAI_CATALOG приоритетнее)."""
    env_cat = os.environ.get("ROUTERAI_CATALOG")
    if env_cat:
        return Path(env_cat)
    try:
        cfg = load_filter_config()
        rel = cfg["paths"]["catalog"]
    except Exception:
        rel = "data/catalog.json"
    return PROJECT_ROOT / rel


def asr_catalog_path() -> Path:
    """Полный путь к data/asr_catalog.json (env ROUTERAI_ASR_CATALOG приоритетнее)."""
    env_cat = os.environ.get("ROUTERAI_ASR_CATALOG")
    if env_cat:
        return Path(env_cat)
    try:
        cfg = load_filter_config()
        rel = cfg["paths"]["asr_catalog"]
    except Exception:
        rel = "data/asr_catalog.json"
    return PROJECT_ROOT / rel


def asr_stale_after_hours() -> float:
    """Максимальный возраст ASR-каталога (часов), после которого запись запрещена."""
    env = os.environ.get("ROUTERAI_ASR_STALE_HOURS")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        return float(load_filter_config().get("asr", {}).get("stale_after_hours", DEFAULT_STALE_HOURS))
    except Exception:
        return DEFAULT_STALE_HOURS


def stale_after_hours() -> float:
    """Максимальный возраст каталога (часов), после которого запись запрещена."""
    env = os.environ.get("ROUTERAI_STALE_HOURS")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        return float(load_filter_config().get("ai_core", {}).get("stale_after_hours", DEFAULT_STALE_HOURS))
    except Exception:
        return DEFAULT_STALE_HOURS


def catalog_age_hours(catalog_file: Optional[Path] = None) -> Optional[float]:
    """Возраст каталога в часах (mtime). None если файл не существует."""
    path = catalog_file or catalog_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    now = datetime.now(timezone.utc).timestamp()
    return (now - mtime) / 3600.0


def is_catalog_stale(catalog_file: Optional[Path] = None) -> bool:
    """True если каталог старше допустимого (или отсутствует)."""
    age = catalog_age_hours(catalog_file)
    if age is None:
        return True
    return age > stale_after_hours()


def catalog_too_small(catalog: list[Any], min_models: int = 1) -> bool:
    """True если каталог подозрительно пуст (пустой ответ API и т.п.)."""
    return not isinstance(catalog, list) or len(catalog) < min_models


def check_catalog_ok(
    catalog_file: Optional[Path] = None,
    min_models: int = 1,
    verbose: bool = True,
) -> bool:
    """Проверка каталога перед записью: существует, не пуст, не устарел.

    Returns:
        True — каталог можно использовать для записи конфигов.
    """
    path = catalog_file or catalog_path()
    if not path.exists():
        if verbose:
            sys.stderr.write(f"[check] Ошибка: каталог не найден: {path}\n")
        return False

    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        if verbose:
            sys.stderr.write(f"[check] Ошибка: каталог {path} повреждён ({exc})\n")
        return False

    models = data.get("models", []) if isinstance(data, dict) else data
    if catalog_too_small(models, min_models):
        if verbose:
            sys.stderr.write(
                f"[check] Ошибка: каталог {path} содержит {len(models)} моделей "
                f"(минимум {min_models}) — запись отменена\n"
            )
        return False

    age = catalog_age_hours(path)
    limit = stale_after_hours()
    if age is not None and age > limit:
        if verbose:
            sys.stderr.write(
                f"[check] Ошибка: каталог {path} устарел "
                f"({age:.1f} ч > {limit:.0f} ч) — запись отменена. "
                f"Запустите fetch_models.py && filter_models.py && build_report.py\n"
            )
        return False
    return True


def check_asr_catalog_ok(
    asr_file: Optional[Path] = None,
    min_models: int = 1,
    verbose: bool = True,
) -> bool:
    """Проверка ASR-каталога перед записью: существует, не пуст, не устарел.

    Аналог check_catalog_ok, но для data/asr_catalog.json с собственным
    порогом свежести (asr.stale_after_hours в filter_config.json).

    Returns:
        True — ASR-каталог можно использовать для записи конфигов.
    """
    path = asr_file or asr_catalog_path()
    if not path.exists():
        if verbose:
            sys.stderr.write(f"[check] Ошибка: ASR-каталог не найден: {path}\n")
        return False

    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        if verbose:
            sys.stderr.write(f"[check] Ошибка: ASR-каталог {path} повреждён ({exc})\n")
        return False

    models = data.get("models", []) if isinstance(data, dict) else data
    if catalog_too_small(models, min_models):
        if verbose:
            sys.stderr.write(
                f"[check] Ошибка: ASR-каталог {path} содержит {len(models)} моделей "
                f"(минимум {min_models}) — запись отменена\n"
            )
        return False

    age = catalog_age_hours(path)
    limit = asr_stale_after_hours()
    if age is not None and age > limit:
        if verbose:
            sys.stderr.write(
                f"[check] Ошибка: ASR-каталог {path} устарел "
                f"({age:.1f} ч > {limit:.0f} ч) — запись отменена. "
                f"Запустите fetch_models.py && filter_models.py && build_report.py\n"
            )
        return False
    return True


def token_limits() -> dict[str, dict[str, int]]:
    """Потолки токенов по тирам из filter_config.json → token_limits.

    Возвращает {"max_input_tokens": {tier: n, ...},
                "max_output_tokens": {tier: n, ...}} с дефолтами, если
    секция отсутствует или тир не указан.
    """
    try:
        cfg = load_filter_config()
        tl = cfg.get("token_limits") or {}
        max_in = tl.get("max_input_tokens") or {}
        max_out = tl.get("max_output_tokens") or {}
        return {
            "max_input_tokens": {
                **DEFAULT_TOKEN_LIMITS["max_input_tokens"],
                **{k: int(v) for k, v in max_in.items()},
            },
            "max_output_tokens": {
                **DEFAULT_TOKEN_LIMITS["max_output_tokens"],
                **{k: int(v) for k, v in max_out.items()},
            },
        }
    except Exception:
        return {k: dict(v) for k, v in DEFAULT_TOKEN_LIMITS.items()}


def apply_token_limits(
    context_length: Any,
    tier: Any,
) -> tuple[int, int]:
    """Считает maxInputTokens/maxOutputTokens для записи модели в конфиг.

    База — консервативная оценка от полного контекста: 80% на вход,
    остаток на выход (но не меньше 2048). Затем применяется жёсткий
    потолок по тиру (token_limits.*): для cheap/medium/premium —
    250K/150K/100K на вход и 16K/8K/8K на выход.

    Args:
        context_length: полный context_length модели из каталога (или None).
        tier: тир модели (cheap/medium/premium/...).

    Returns:
        (max_input_tokens, max_output_tokens) — целые числа.
    """
    limits = token_limits()
    max_in_by_tier = limits["max_input_tokens"]
    max_out_by_tier = limits["max_output_tokens"]

    try:
        ctx = int(context_length) if context_length else DEFAULT_CONTEXT_LENGTH
    except (TypeError, ValueError):
        ctx = DEFAULT_CONTEXT_LENGTH

    # База: 80% на вход / 20% на выход (консервативная оценка)
    max_input = int(ctx * 0.8)
    max_output = max(2048, ctx - max_input)

    # Жёсткий потолок по тиру
    if tier in max_in_by_tier:
        max_input = min(max_input, int(max_in_by_tier[tier]))
    if tier in max_out_by_tier:
        max_output = min(max_output, int(max_out_by_tier[tier]))

    return max_input, max_output
