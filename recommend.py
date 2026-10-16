#!/usr/bin/env python3
"""Рекомендации по выбору моделей routerai.ru: скоринг fit-to-purpose + категории.

Читает:
  - data/catalog.json         — полный каталог валидных чат-моделей
  - filter_config.json        — пороги, эталон, параметры скоринга
  - data/config_models.json   — формат ai-core (опционально, для пометки "в конфиге")
  - imports/chatLanguageModels.json — VS Code (опционально, для пометки "в clm")

Пишет:
  - data/recommendations.json — скоринг, категории, топ-N по категориям

Категории использования (fit-to-purpose):
  - default_agent        — основной агент (дёшево, надёжно, tools)
  - planner             — мультимодельное планирование (дёшево + качество)
  - reviewer            — ревью кода / ответов (рекомендуется reasoning)
  - synthesis           — синтез планов в один (быстро, дёшево)
  - browser_automation  — автоматизация браузера (дёшево, быстрые вызовы)
  - coding              — генерация и рефакторинг кода (кодеры)
  - long_context        — большие контексты (>= 512K)
  - cheap_workhorse     — универсальная дешёвая рабочая лошадка

Оценка: 0..100. Базовая 50 + бонусы/штрафы. Подробности в параметрах
скоринга filter_config.json -> scoring.

Использование:
    python3 recommend.py [--catalog PATH] [--config-models PATH] [--clm PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

from ai_core_paths import ai_core_config_path  # noqa: E402

from reliability import (  # noqa: E402
    is_model_approved,
    resolve_reference,
)

# Ключевые слова (id и/или name) для категорийного бонуса кодерам.
# Используется ТОЛЬКО в категории coding (см. CATEGORY_DEFS.coding.coding_bonus).
CODING_KEYWORDS = ("coder", "coding", "codex", "code")

# Категории: определение + параметры приоритета (порядок важен для топ-N)
CATEGORY_DEFS: dict[str, dict[str, Any]] = {
    "default_agent": {
        "label": "Основной агент",
        "desc": "Дёшево, надёжно, tools, контекст не критичен",
        "tiers": ["cheap"],
        "min_ctx": 64_000,
        "weight_cost": 1.4,
        "weight_quality": 0.4,
    },
    "planner": {
        "label": "Планирование (multi-model planning)",
        "desc": "Дёшево + достаточно ума для декомпозиции задач",
        "tiers": ["cheap", "medium"],
        "min_ctx": 96_000,
        "weight_cost": 1.0,
        "weight_quality": 1.0,
    },
    "reviewer": {
        "label": "Ревью кода / ответов",
        "desc": "Reasoning-модели, цена вторична",
        "tiers": ["cheap", "medium", "premium"],
        "min_ctx": 64_000,
        "weight_cost": 0.3,
        "weight_quality": 1.6,
        "prefer_reasoning": True,
    },
    "synthesis": {
        "label": "Синтез планов",
        "desc": "Быстро, дёшево, большая точность инструкций",
        "tiers": ["cheap"],
        "min_ctx": 64_000,
        "weight_cost": 1.3,
        "weight_quality": 0.6,
    },
    "browser_automation": {
        "label": "Автоматизация браузера",
        "desc": "Дёшево, маленькая задержка, много коротких вызовов",
        "tiers": ["cheap"],
        "min_ctx": 32_000,
        "weight_cost": 1.5,
        "weight_quality": 0.3,
    },
    "coding": {
        "label": "Генерация / рефакторинг кода",
        "desc": "Кодеры с большим контекстом",
        "tiers": ["cheap", "medium", "premium"],
        "min_ctx": 96_000,
        "weight_cost": 0.8,
        "weight_quality": 1.2,
        "coding_bonus": 12,
    },
    "long_context": {
        "label": "Большой контекст",
        "desc": ">= 512K токенов",
        "tiers": ["cheap", "medium", "premium"],
        "min_ctx": 512_000,
        "weight_cost": 0.5,
        "weight_quality": 0.8,
    },
    "cheap_workhorse": {
        "label": "Универсальная дешёвая лошадка",
        "desc": "Близость к эталону по цене и контексту",
        "tiers": ["cheap"],
        "min_ctx": 32_000,
        "weight_cost": 1.6,
        "weight_quality": 0.3,
    },
}


# --------------------------------------------------------------------------- #
# Загрузка
# --------------------------------------------------------------------------- #
def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_catalog(path: Path) -> list[dict[str, Any]]:
    data = load_json(path)
    return data.get("models", [])


# --------------------------------------------------------------------------- #
# Скоринг
# --------------------------------------------------------------------------- #
def novelty_score(
    created: Any,
    scoring: dict[str, Any],
) -> tuple[float, str]:
    """Оценивает новизну модели по дате создания (unix-секунды в `created`).

    Возвращает (бонус 0..max_novelty_bonus, человекочитаемая метка).

    Логика:
      - свежие (возраст <= novelty_window_days) получают полный бонус;
      - далее плавный линейный спад до 0 к novelty_decay_days;
      - модели без даты — 0 (нейтрально, без штрафа).
    """
    max_bonus = float(scoring.get("max_novelty_bonus", 8))
    window = float(scoring.get("novelty_window_days", 90))
    decay = float(scoring.get("novelty_decay_days", 730))

    if not created:
        return 0.0, "дата неизвестна"

    try:
        created_dt = datetime.fromtimestamp(float(created), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return 0.0, "дата некорректна"

    age_days = (datetime.now(timezone.utc) - created_dt).days
    age_days = max(0, age_days)

    if age_days <= window:
        bonus = max_bonus
        label = f"новая ({age_days} дн.)"
    elif age_days >= decay:
        bonus = 0.0
        label = f"возраст {age_days} дн."
    else:
        # линейный спад от window до decay
        fraction = 1.0 - (age_days - window) / (decay - window)
        bonus = max_bonus * fraction
        label = f"возраст {age_days} дн."

    return bonus, label


def score_model(
    m: dict[str, Any],
    scoring: dict[str, Any],
    cat_def: dict[str, Any],
    reference: dict[str, Any],
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Считает score fit-to-purpose для одной модели в одной категории.

    score = 50 (база)
            + cost_bonus        (0..30, чем дешевле эталона — тем больше)
            + quality_bonus     (0..15, контекст, reasoning, tools)
            + novelty_bonus     (0..min(8, 20% базы), свежесть по created)
            + coding_bonus      (0..12, категорийный критерий для coding)
            + reference_bonus   (только если эталон утверждён, +5)
            - obsolete_penalty  (старые семейства, кроме approved)
    """
    base = float(scoring.get("base_score", 50))
    max_cost = float(scoring.get("max_cost_bonus", 30))
    max_quality = float(scoring.get("max_quality_bonus", 15))

    w_cost = float(cat_def.get("weight_cost", 1.0))
    w_quality = float(cat_def.get("weight_quality", 1.0))

    cost = m.get("cost_per_turn_estimate_rub") or 0.0
    ref_cost = reference.get("cost_per_turn_estimate_rub") or 0.0

    # Стоимость: нормализуем относительно эталона, обратная зависимость
    if ref_cost > 0:
        ratio = cost / ref_cost
    else:
        ratio = 1.0
    # ratio=1 (как эталон) -> 0.5; ratio=0.1 -> ~0.9; ratio=10 -> ~0.09
    cost_score = 1.0 / (1.0 + ratio)
    cost_bonus = max_cost * cost_score * w_cost

    # Качество: контекст, tools, reasoning, structured outputs
    ctx = m.get("context_length") or 0
    ref_ctx = reference.get("context_length") or 0
    quality = 0.0
    quality += min(1.0, ctx / max(1, ref_ctx)) * 0.35          # контекст vs эталон
    if m.get("supports_tools"):
        quality += 0.2
    if m.get("supports_structured_outputs"):
        quality += 0.15
    if m.get("has_reasoning"):
        quality += 0.2
    # длинный контекст приятен
    if ctx >= 512_000:
        quality += 0.1
    quality_bonus = max_quality * min(1.0, quality) * w_quality

    # Новизна модели (свежесть по дате created)
    novelty_bonus, novelty_label = novelty_score(m.get("created"), scoring)

    # Потолок новизны: бонус не может превышать ceiling_ratio от базы
    # (по умолчанию 20%). Предотвращает перевес свежести над стоимостью.
    ceiling_ratio = float(scoring.get("novelty_ceiling_ratio", 0.2))
    novelty_ceiling = base * ceiling_ratio
    if novelty_bonus > novelty_ceiling:
        novelty_bonus = novelty_ceiling

    score = base + cost_bonus + quality_bonus + novelty_bonus

    # Бонус кодеров для категории coding: категорийный критерий —
    # id ИЛИ name содержит ключевые слова (coder/coding/codex/code).
    # Задокументировано в CODING_KEYWORDS; агентные модели ("agent")
    # сюда не входят — это универсальные ассистенты, не кодеры.
    coding_bonus = 0.0
    if cat_def.get("coding_bonus"):
        haystack = " ".join(
            str(x).lower() for x in (m.get("id"), m.get("name")) if x
        )
        if any(kw in haystack for kw in CODING_KEYWORDS):
            coding_bonus = float(cat_def["coding_bonus"])
            score += coding_bonus

    # Эталон — небольшой бонус, но ТОЛЬКО если эталон утверждён
    # (прошёл гейт надёжности reference_model.approved_ids).
    # Временный авто-эталон бонуса не получает, чтобы не искажать рейтинг.
    reference_bonus = 0.0
    if reference and m.get("id") == reference.get("id") and is_model_approved(
        m.get("id"), cfg
    ):
        reference_bonus = 5.0
        score += reference_bonus

    # Предпочтение reasoning для reviewer
    if cat_def.get("prefer_reasoning") and not m.get("has_reasoning"):
        score -= 6.0

    # Штраф за "старые" семейства (см. filter_config.json -> scoring.obsolete_penalty).
    # Поколения-снапшоты (например deepseek-v3.1/v3.2) штрафуются, если модель
    # НЕ прошла гейт надёжности (approved_ids) — утверждённые не штрафуются.
    penalty = 0.0
    model_id = m.get("id", "")
    for pattern in scoring.get("obsolete_patterns", []):
        if pattern in model_id and not is_model_approved(model_id, cfg):
            penalty = float(scoring.get("obsolete_penalty", 0))
            break
    score -= penalty

    return {
        "score": round(score, 1),
        "components": {
            "base": base,
            "cost_bonus": round(cost_bonus, 1),
            "quality_bonus": round(quality_bonus, 1),
            "novelty_bonus": round(novelty_bonus, 1),
            "coding_bonus": round(coding_bonus, 1),
            "reference_bonus": reference_bonus,
            "obsolete_penalty": -penalty,
        },
        "reasons": build_reasons(m, cost, ref_cost, ctx, ref_ctx, novelty_bonus, novelty_label),
    }


def build_reasons(
    m: dict[str, Any],
    cost: float,
    ref_cost: float,
    ctx: int,
    ref_ctx: int,
    novelty_bonus: float = 0.0,
    novelty_label: str = "",
) -> list[str]:
    """Краткие человекочитаемые причины оценки."""
    reasons: list[str] = []
    if ref_cost > 0:
        ratio = cost / ref_cost
        if ratio <= 0.5:
            reasons.append(f"стоимость {ratio:.1f}× дешевле эталона")
        elif ratio <= 1.5:
            reasons.append("стоимость на уровне эталона")
        else:
            reasons.append(f"дороже эталона в {ratio:.1f}×")
    if ctx >= ref_ctx:
        reasons.append("контекст не меньше эталона")
    elif ctx >= 128_000:
        reasons.append(f"контекст {ctx // 1000}K")
    if m.get("supports_tools"):
        reasons.append("поддержка tools")
    if m.get("supports_structured_outputs"):
        reasons.append("структурные ответы")
    if m.get("supports_vision"):
        reasons.append("vision (изображения на входе)")
    if m.get("has_reasoning"):
        reasons.append("reasoning")
    if any(kw in " ".join(str(x).lower() for x in (m.get("id"), m.get("name")) if x)
           for kw in CODING_KEYWORDS):
        reasons.append("кодер")
    if novelty_bonus > 0:
        reasons.append(novelty_label)
    return reasons


# --------------------------------------------------------------------------- #
# Категории
# --------------------------------------------------------------------------- #
def build_categories(
    catalog: list[dict[str, Any]],
    scoring: dict[str, Any],
    reference: dict[str, Any],
    known_in_config: set[str],
    known_in_clm: set[str],
    cfg: Optional[dict[str, Any]] = None,
    top_n: int = 10,
    excluded_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Считает скоринг по всем категориям и собирает топ-N по каждой.

    excluded_ids — модели, исключённые из выбора (sync.selection.excluded_ids):
    они не участвуют ни в одном рейтинге. Синхронизировано с select_top_models()
    в sync_configs.py (excluded модели не попадают в топ-N ни в одну роль).
    """
    excluded_ids = excluded_ids or set()
    result: dict[str, Any] = {}

    for cat_id, cat_def in CATEGORY_DEFS.items():
        allowed_tiers = set(cat_def["tiers"])
        min_ctx = cat_def["min_ctx"]

        scored: list[dict[str, Any]] = []
        for m in catalog:
            if m.get("id") in excluded_ids:
                continue
            if m.get("tier") not in allowed_tiers:
                continue
            if (m.get("context_length") or 0) < min_ctx:
                continue
            s = score_model(m, scoring, cat_def, reference, cfg)
            scored.append({
                "id": m["id"],
                "tier": m.get("tier"),
                "provider": m.get("provider"),
                "prompt_rub": m.get("prompt_rub"),
                "completion_rub": m.get("completion_rub"),
                "cost_per_turn_estimate_rub": m.get("cost_per_turn_estimate_rub"),
                "context_length": m.get("context_length"),
                "created": m.get("created"),
                "has_reasoning": bool(m.get("has_reasoning")),
                "supports_tools": bool(m.get("supports_tools")),
                "supports_vision": bool(m.get("supports_vision")),
                "in_config": m["id"] in known_in_config,
                "in_clm": m["id"] in known_in_clm,
                **s,
            })

        # Сортировка по score desc, затем по стоимости asc
        scored.sort(key=lambda x: (-x["score"], x["cost_per_turn_estimate_rub"] or 0))

        result[cat_id] = {
            "label": cat_def["label"],
            "description": cat_def["desc"],
            "tiers": sorted(allowed_tiers),
            "min_context": min_ctx,
            "top": scored[:top_n],
            "total": len(scored),
        }

    return result


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=None)
    parser.add_argument("--config-models", type=Path, default=None)
    parser.add_argument("--clm", type=Path, default=None)
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args(argv)

    try:
        fcfg = load_json(PROJECT_ROOT / "filter_config.json")
    except FileNotFoundError:
        sys.stderr.write("Ошибка: не найден filter_config.json\n")
        return 1

    paths = fcfg["paths"]
    catalog_path = args.catalog or (PROJECT_ROOT / paths["catalog"])
    clm_path = args.clm or (PROJECT_ROOT / paths["clm_import"])

    try:
        catalog = load_catalog(catalog_path)
    except FileNotFoundError:
        sys.stderr.write(f"Ошибка: не найден каталог {catalog_path}. Сначала запустите build_report.py\n")
        return 1

    # Известные модели: реальный конфиг ai-core (llm.providers.routerai.models)
    known_in_config: set[str] = set()
    ai_core_cfg_path = ai_core_config_path()
    try:
        ai_core_cfg = load_json(ai_core_cfg_path)
        routerai = (ai_core_cfg.get("llm", {}).get("providers", {}) or {}).get("routerai")
        if routerai:
            known_in_config |= {m.get("id") for m in routerai.get("models", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    known_in_clm: set[str] = set()
    try:
        clm = load_json(clm_path)
        # clm — либо список провайдеров, либо список моделей
        if isinstance(clm, list) and clm and "models" in clm[0]:
            for prov in clm:
                known_in_clm |= {m.get("id") for m in prov.get("models", [])}
        else:
            known_in_clm = {m.get("id") for m in clm}
    except FileNotFoundError:
        pass

    scoring = fcfg.get("scoring", {})
    # Эталон: гейт надёжности — если reference_model.id не утверждён
    # (approved_ids), выбираем самую дешёвую cheap+tools вне watchlist.
    resolution = resolve_reference(fcfg, catalog)
    reference_id = resolution["id"]
    by_id = {m["id"]: m for m in catalog}
    reference = by_id.get(reference_id) if reference_id else None
    if not reference:
        sys.stderr.write(
            f"Ошибка: эталон не выбран (status={resolution['status']}). "
            "Каталог пуст или все cheap-модели с tools в watchlist.\n"
        )
        return 1

    categories = build_categories(
        catalog, scoring, reference, known_in_config, known_in_clm, fcfg, args.top_n,
        excluded_ids=set((fcfg.get("sync") or {}).get("selection", {}).get("excluded_ids") or []),
    )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reference_model": reference_id,
        "reference_cost_rub": reference.get("cost_per_turn_estimate_rub"),
        "reference_resolution": resolution,
        "categories": categories,
        "top_n": args.top_n,
        "stats": {
            "catalog_models": len(catalog),
            "in_config": len(known_in_config),
            "in_clm": len(known_in_clm),
        },
    }

    out_path = PROJECT_ROOT / paths["recommendations"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    tmp.replace(out_path)

    # Консольный отчёт
    print("=" * 72)
    print("РЕКОМЕНДАЦИИ по моделям routerai.ru (fit-to-purpose)")
    print("=" * 72)
    print(f"Эталон: {reference_id} (cost={reference.get('cost_per_turn_estimate_rub'):.4f}₽/turn, "
          f"статус={resolution['status']})")
    print(f"Каталог: {len(catalog)} моделей, в конфиге ai-core: {len(known_in_config)}, в clm: {len(known_in_clm)}")
    print()
    for cat_id, cat in categories.items():
        tiers_str = "/".join(cat["tiers"])
        print(f"◆ {cat['label']}  [{tiers_str}]")
        print(f"  {cat['description']} (моделей в выборке: {cat['total']})")
        for i, m in enumerate(cat["top"][:5], 1):
            flags = []
            if m["in_config"]:
                flags.append("конфиг")
            if m["in_clm"]:
                flags.append("clm")
            if m["has_reasoning"]:
                flags.append("🧠")
            extra = f" [{'/'.join(flags)}]" if flags else ""
            age = ""
            if m.get("created"):
                age_days = max(0, (datetime.now(timezone.utc) - datetime.fromtimestamp(m["created"], tz=timezone.utc)).days)
                if age_days <= 90:
                    age = f" 🆕{age_days}дн"
            print(f"   {i}. {m['id']:<45} score={m['score']:>5} cost={m['cost_per_turn_estimate_rub']:.4f}₽{age}{extra}")
        print()

    print(f"[recommend] Файл: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
