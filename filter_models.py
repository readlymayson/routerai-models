#!/usr/bin/env python3
"""Валидация и тиринг чат-моделей каталога routerai.ru.

Читает data/routerai_models_raw.json и формирует data/filtered_models.json:

1. Валидация чат-моделей:
   - на входе есть text, на выходе есть text
     (modality_mode='text_in_text_out' — допускает image/audio/video на входе);
   - поддержка параметра 'tools'
   - ненулевые цены pricing.prompt и pricing.completion

2. Непересекающиеся тиры (пороги из filter_config.json, в рублях за 1M токенов):
   - cheap   : prompt < 100 И completion < 100 (рабочие лошадки агентов)
   - premium : has_reasoning И (prompt >= 100 ИЛИ completion >= 100)
   - medium  : все остальные валидные модели

3. Расчет cost_per_turn_estimate для типового шага агента
   (prompt_tokens / 1M * prompt_price + completion_tokens / 1M * completion_price).

Использование:
    python3 filter_models.py [--raw data/routerai_models_raw.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
TIER_ORDER = ["cheap", "medium", "premium"]


# --------------------------------------------------------------------------- #
# Загрузка данных
# --------------------------------------------------------------------------- #
def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config() -> dict[str, Any]:
    return load_json(PROJECT_ROOT / "filter_config.json")


def load_raw_models(raw_path: Path) -> list[dict[str, Any]]:
    """Возвращает список сырых моделей из файла выгрузки."""
    payload = load_json(raw_path)
    if isinstance(payload, dict):
        items = payload.get("data", [])
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError(f"Файл {raw_path} не содержит списка моделей")
    return items


# --------------------------------------------------------------------------- #
# Валидация
# --------------------------------------------------------------------------- #
def is_valid_chat_model(
    model: dict[str, Any],
    cfg: dict[str, Any],
) -> bool:
    """Проверяет, является ли модель валидной чат-моделью (text + tools)."""
    val_cfg = cfg.get("validation", {})
    modality_mode = val_cfg.get("modality_mode", "text->text")
    require_tools = val_cfg.get("require_tools_param", True)
    require_positive = val_cfg.get("require_positive_pricing", True)

    if not matches_modality(model, modality_mode):
        return False

    if require_tools and "tools" not in (model.get("supported_parameters") or []):
        return False

    pricing = model.get("pricing") or {}
    if require_positive:
        prompt = pricing.get("prompt")
        completion = pricing.get("completion")
        if not prompt or not completion or prompt <= 0 or completion <= 0:
            return False

    return True


def has_reasoning(model: dict[str, Any]) -> bool:
    """Определяет поддержку режима рассуждений (reasoning)."""
    params = model.get("supported_parameters") or []
    return "reasoning" in params or "include_reasoning" in params


def get_modality(model: dict[str, Any]) -> str:
    """Возвращает строку модальности модели (например 'text+image->text')."""
    arch = model.get("architecture") or {}
    return arch.get("modality") or "unknown"


def input_modalities(model: dict[str, Any]) -> list[str]:
    """Список входных модальностей ('text', 'image', 'audio', ...)."""
    arch = model.get("architecture") or {}
    return list(arch.get("input_modalities") or [])


def output_modalities(model: dict[str, Any]) -> list[str]:
    """Список выходных модальностей."""
    arch = model.get("architecture") or {}
    return list(arch.get("output_modalities") or [])


def supports_vision(model: dict[str, Any]) -> bool:
    """Поддерживает ли модель изображения на входе (vision)."""
    return "image" in input_modalities(model)


def matches_modality(model: dict[str, Any], mode: str) -> bool:
    """Проверяет модель на соответствие режиму модальности.

    Режимы (filter_config.json → validation.modality_mode):
      - "text->text"      — строго текст на входе и выходе (по `modality`-строке)
      - "text_in_text_out" — на входе есть text, на выходе есть text
                             (допускает image/audio/video на входе — vision)
    """
    if mode == "text_in_text_out":
        ins = input_modalities(model)
        outs = output_modalities(model)
        if not ins and not outs:
            # фолбэк на строку modality (старые данные без списков)
            mod = get_modality(model)
            return "->text" in mod and mod.startswith("text")
        return "text" in ins and "text" in outs
    # по умолчанию — строгое сравнение строки modality
    return get_modality(model) == mode


# --------------------------------------------------------------------------- #
# Тиринг и стоимость
# --------------------------------------------------------------------------- #
def rub_per_1m(price_per_token: float) -> float:
    """Переводит цену за 1 токен в рубли за 1M токенов."""
    return price_per_token * 1_000_000.0


def assign_tier(model: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Присваивает модели непересекающийся тир: cheap | medium | premium.

    Порядок проверки гарантирует непересечение:
      1. cheap  — оба порога ниже максимума
      2. premium — есть reasoning И хотя бы один порог >= минимума
      3. medium — всё остальное
    """
    tiers_cfg = cfg.get("tiers", {})
    pricing = model.get("pricing") or {}
    prompt_rub = rub_per_1m(pricing.get("prompt", 0.0))
    completion_rub = rub_per_1m(pricing.get("completion", 0.0))

    cheap_cfg = tiers_cfg.get("cheap", {})
    if (
        prompt_rub < cheap_cfg.get("max_prompt_rub_per_1m", 100.0)
        and completion_rub < cheap_cfg.get("max_completion_rub_per_1m", 100.0)
    ):
        return "cheap"

    premium_cfg = tiers_cfg.get("premium", {})
    if (
        has_reasoning(model)
        and (
            prompt_rub >= premium_cfg.get("min_prompt_rub_per_1m", 100.0)
            or completion_rub >= premium_cfg.get("min_completion_rub_per_1m", 100.0)
        )
    ):
        return "premium"

    return "medium"


def calculate_turn_cost(model: dict[str, Any], cfg: dict[str, Any]) -> float:
    """Считает ориентировочную стоимость типового шага агента в рублях.

    Формула:
        cost = prompt_tokens/1M * prompt_rub_per_1m
             + completion_tokens/1M * completion_rub_per_1m
    """
    call_cfg = cfg.get("call_params", {})
    prompt_tokens = call_cfg.get("prompt_tokens", 10_000)
    completion_tokens = call_cfg.get("completion_tokens", 1_000)
    pricing = model.get("pricing") or {}
    prompt_rub = rub_per_1m(pricing.get("prompt", 0.0))
    completion_rub = rub_per_1m(pricing.get("completion", 0.0))

    cost = (prompt_tokens / 1_000_000.0) * prompt_rub + (
        completion_tokens / 1_000_000.0
    ) * completion_rub
    return round(cost, 6)


# --------------------------------------------------------------------------- #
# Основной конвейер
# --------------------------------------------------------------------------- #
def filter_and_categorize(
    models: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Валидирует модели, присваивает тиры и считает стоимость вызова.

    Returns:
        Словарь:
            {
              "generated_at": str,
              "stats": {"valid": int, "cheap": int, "medium": int, "premium": int,
                        "invalid": int, "excluded_by_modality": int, ...},
              "reference_model": {...} | None,
              "tiers": {"cheap": [...], "medium": [...], "premium": [...]}
            }
    """
    tiers_cfg = cfg.get("tiers", {})
    reference_id = (cfg.get("reference_model") or {}).get("id")

    valid: list[dict[str, Any]] = []
    stats = {
        "total_raw": len(models),
        "valid": 0,
        "invalid": 0,
        "excluded_by_modality": 0,
        "excluded_no_tools": 0,
        "excluded_no_pricing": 0,
    }
    tiers: dict[str, list[dict[str, Any]]] = {t: [] for t in TIER_ORDER}

    modality_mode = cfg.get("validation", {}).get("modality_mode", "text->text")

    for m in models:
        arch = m.get("architecture") or {}
        params = m.get("supported_parameters") or []
        pricing = m.get("pricing") or {}

        if not matches_modality(m, modality_mode):
            stats["excluded_by_modality"] += 1
            continue
        if "tools" not in params:
            stats["excluded_no_tools"] += 1
            continue
        if not pricing.get("prompt") or not pricing.get("completion") or pricing["prompt"] <= 0 or pricing["completion"] <= 0:
            stats["excluded_no_pricing"] += 1
            continue

        if not is_valid_chat_model(m, cfg):
            stats["invalid"] += 1
            continue

        tier = assign_tier(m, cfg)
        entry = {
            "id": m.get("id"),
            "name": m.get("name"),
            "created": m.get("created"),
            "context_length": m.get("context_length"),
            "description_short": (m.get("description") or "")[:300],
            "has_reasoning": has_reasoning(m),
            "supports_structured_outputs": "structured_outputs" in params,
            "supports_tools": "tools" in params,
            "modality": get_modality(m),
            "input_modalities": input_modalities(m),
            "output_modalities": output_modalities(m),
            "supports_vision": supports_vision(m),
            "pricing_rub_per_1m": {
                "prompt": round(rub_per_1m(pricing.get("prompt", 0.0)), 4),
                "completion": round(rub_per_1m(pricing.get("completion", 0.0)), 4),
                "input_cache_read": (
                    round(rub_per_1m(pricing["input_cache_read"]), 4)
                    if pricing.get("input_cache_read")
                    else None
                ),
            },
            "cost_per_turn_estimate_rub": calculate_turn_cost(m, cfg),
            "tier": tier,
            "tier_label": tiers_cfg.get(tier, {}).get("description", tier),
        }
        valid.append(entry)
        tiers[tier].append(entry)

    # Сортировка внутри тиров: сначала по цене prompt, затем по completion
    for t in TIER_ORDER:
        tiers[t].sort(key=lambda e: (e["pricing_rub_per_1m"]["prompt"], e["pricing_rub_per_1m"]["completion"], e["id"]))

    # Валидация непересечения тиров
    seen_ids: set[str] = set()
    for t in TIER_ORDER:
        for e in tiers[t]:
            if e["id"] in seen_ids:
                raise RuntimeError(f"Модель {e['id']} попала в несколько тиров!")
            seen_ids.add(e["id"])
    assert len(seen_ids) == len(valid), "Потеря моделей при распределении по тирам"

    # Эталонная модель
    reference = next((e for e in valid if e["id"] == reference_id), None)

    stats["valid"] = len(valid)
    stats["cheap"] = len(tiers["cheap"])
    stats["medium"] = len(tiers["medium"])
    stats["premium"] = len(tiers["premium"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "reference_model": reference,
        "tiers": tiers,
    }


def save_filtered(result: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    tmp.replace(out_path)


# --------------------------------------------------------------------------- #
# ASR-модели (audio -> transcription)
# --------------------------------------------------------------------------- #
def is_asr_model(model: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Является ли модель ASR (распознавание речи).

    Критерий: на входе есть audio, на выходе есть transcription
    (по спискам input_modalities/output_modalities; фолбэк — строка modality
    вида 'audio->transcription').
    """
    asr_cfg = cfg.get("asr", {})
    out_mod = asr_cfg.get("output_modality", "transcription")
    ins = input_modalities(model)
    outs = output_modalities(model)
    if ins or outs:
        return "audio" in ins and out_mod in outs
    return get_modality(model) == f"audio->{out_mod}"


def rub_per_min(model: dict[str, Any]) -> Optional[float]:
    """Цена за минуту аудио (₽/мин) из pricing.seconds.

    Returns:
        Цена за минуту или None, если pricing.seconds отсутствует.
    """
    pricing = model.get("pricing") or {}
    sec = pricing.get("seconds")
    if not sec or sec <= 0:
        return None
    return round(sec * 60.0, 6)


def filter_asr_models(
    models: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Отбирает ASR-модели и считает их стоимость за минуту.

    Returns:
        Словарь:
            {
              "generated_at": str,
              "stats": {"total_raw": int, "asr": int, "no_pricing": int},
              "models": [ {id, name, created, description_short,
                           input_modalities, output_modalities,
                           rub_per_min, rub_per_min_label,
                           tier (cheap/medium), tier_label}, ... ]
            }
    """
    asr_cfg = cfg.get("asr", {})
    max_cheap = float(asr_cfg.get("max_rub_per_min_cheap", 0.5))
    tiers_cfg = cfg.get("tiers", {})

    entries: list[dict[str, Any]] = []
    stats = {"total_raw": len(models), "asr": 0, "no_pricing": 0}

    for m in models:
        if not is_asr_model(m, cfg):
            continue
        rpm = rub_per_min(m)
        if rpm is None:
            stats["no_pricing"] += 1
            continue
        tier = "cheap" if rpm <= max_cheap else "medium"
        entries.append(
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "created": m.get("created"),
                "description_short": (m.get("description") or "")[:300],
                "input_modalities": input_modalities(m),
                "output_modalities": output_modalities(m),
                "rub_per_min": rpm,
                "rub_per_min_label": f"{rpm:.4f} ₽/мин",
                "tier": tier,
                "tier_label": (
                    "Дешёвые (≤ %.2f ₽/мин)" % max_cheap
                    if tier == "cheap"
                    else tiers_cfg.get("medium", {}).get("description", "medium")
                ),
            }
        )

    entries.sort(key=lambda e: (e["rub_per_min"], e["id"]))
    stats["asr"] = len(entries)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "models": entries,
    }


def save_asr(result: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    tmp.replace(out_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw",
        type=Path,
        default=None,
        help="Путь к сырым данным (по умолчанию из filter_config.json)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except FileNotFoundError:
        sys.stderr.write("Ошибка: не найден filter_config.json\n")
        return 1

    raw_path = args.raw or (PROJECT_ROOT / cfg["paths"]["raw_data"])
    try:
        models = load_raw_models(raw_path)
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"Ошибка: {exc}\n")
        return 1

    result = filter_and_categorize(models, cfg)
    out_path = PROJECT_ROOT / cfg["paths"]["filtered"]
    save_filtered(result, out_path)

    s = result["stats"]
    print(f"[filter] Всего моделей в каталоге: {s['total_raw']}")
    print(f"[filter] Валидных чат-моделей: {s['valid']}")
    print(f"[filter]   cheap   (рабочие лошадки):   {s['cheap']}")
    print(f"[filter]   medium  (без reasoning):     {s['medium']}")
    print(f"[filter]   premium (reasoning/ревью):   {s['premium']}")
    print(f"[filter] Исключено: modality={s['excluded_by_modality']}, "
          f"no_tools={s['excluded_no_tools']}, no_pricing={s['excluded_no_pricing']}")
    ref = result.get("reference_model")
    if ref:
        p = ref["pricing_rub_per_1m"]
        print(f"[filter] Эталон {ref['id']}: prompt={p['prompt']} ₽/1M, "
              f"completion={p['completion']} ₽/1M, turn≈{ref['cost_per_turn_estimate_rub']} ₽")
    print(f"[filter] Сохранено: {out_path}")

    # ASR-модели (audio -> transcription)
    asr_result = filter_asr_models(models, cfg)
    asr_path = PROJECT_ROOT / cfg["paths"]["asr_models"]
    save_asr(asr_result, asr_path)
    asr_stats = asr_result["stats"]
    print(f"[filter] ASR-моделей: {asr_stats['asr']} (без цены за минуту: {asr_stats['no_pricing']})")
    print(f"[filter] Сохранено: {asr_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
