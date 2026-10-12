#!/usr/bin/env python3
"""Актуализация конфигураций: ai-core/config/config.json + VS Code chatLanguageModels.

Что делает:
  1. Читает актуальный каталог data/catalog.json (после build_report.py).
  2. Генерирует imports/chatLanguageModels.json (VS Code формат) из каталога
     (копия для истории/сравнения; сам VS Code читает свой файл отдельно).
  3. Обновляет ai-core/config/config.json:
     - секция llm.providers.routerai.models — актуальные модели из каталога
       (дешёвые приоритетнее; _meta НЕ копируется);
     - переписывает ссылки вида routerai:<id> (default/reviewer/planner/
       synthesizer/browser_automation/.../llm_model) на ближайшие дешёвые
       альтернативы, если текущий id отсутствует в каталоге.
  4. Может обновить сам источник VS Code chatLanguageModels.json (--apply-clm).

Бэкапы НЕ создаются: репозиторий ai-core покрыт git.

Безопасность:
  - --dry-run: только показать дифф, ничего не писать.
  - ВАЖНО: clm_source (VS Code) обновляется ТОЛЬКО с --apply-clm.

Использование:
    python3 sync_configs.py [--dry-run] [--apply-clm] [--no-config]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

from ai_core_paths import (  # noqa: E402
    ai_core_config_path,
    apply_token_limits,
    asr_catalog_path,
    asr_stale_after_hours,
    catalog_age_hours,
    check_asr_catalog_ok,
    check_catalog_ok,
    stale_after_hours,
)

from reliability import (  # noqa: E402
    effective_reference_id,
    is_model_approved,
    is_watchlisted,
)

__dry_run = True  # глобальный флаг для print_diff (устанавливается в main)

# Порядок тиров для сортировки «по умолчанию» (без рекомендаций)
TIER_ORDER = {"cheap": 0, "medium": 1, "premium": 2}

# Веса категорий рекомендаций для гибридного score (можно переопределить
# в filter_config.json → sync.selection.weights)
DEFAULT_SELECTION_WEIGHTS = {
    "default_agent": 1.0,
    "cheap_workhorse": 1.0,
    "reviewer": 0.8,
    "coding": 0.8,
    "planner": 0.6,
    "long_context": 0.6,
    "synthesis": 0.4,
    "browser_automation": 0.4,
}

# Политика провайдера: per-model order of preferred providers and fallback control
# Источник: filter_config.json → provider_policy (если задан, иначе пустой dict)
# Структура:
# {
#   "mode": "per_model",  # "per_model" или "global"
#   "models": {
#     "deepseek/deepseek-v4.1-flash": {"order": ["deepseek", "yandex"], "allow_fallbacks": true},
#     "anthropic/claude-sonnet-4.5": {"order": ["anthropic", "google"]},
#     ...
#   },
#   "default": {"order": [], "allow_fallbacks": true}  # fallback для моделей без записи
# }
# Важно: order проверяется только против endpoint'ов конкретной модели из data/endpoints.json.
# Глобальный order отклонён: разные семейства моделей имеют разных провайдеров.
PROVIDER_POLICY_DEFAULT = {
    "mode": "per_model",
    "models": {},
    "default": {"order": [], "allow_fallbacks": True},
}

# Переопределения цен (₽/токен) для конкретных моделей.
# Формат: { "<full model id>": {prompt_token: float, completion_token: float} }
# Источник: filter_config.json → pricing_overrides (если задан, иначе пустой dict).
# Содержит только базовые токенные тарифы; переменные пороги (variable_pricings) НЕ включаются.
PRICING_OVERRIDES_DEFAULT = {}


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
# Поиск ссылок на модели в конфиге ai-core
# --------------------------------------------------------------------------- #
MODEL_REF_KEYS = {
    "default_model",
    "reviewer_model",
    "synthesizer_model",
    "model",
    "llm_model",
    "planner_models",  # список
}


def find_model_refs(obj: Any, path: str = "", out: list[tuple[str, Any]] | None = None) -> list[tuple[str, Any]]:
    """Рекурсивно находит пути к значениям вида routerai:<id>.

    Возвращает список (путь, значение). Пути в формате a.b[0].c для JSON-патчинга.
    """
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else k
            find_model_refs(v, child, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_model_refs(v, f"{path}[{i}]", out)
    elif isinstance(obj, str) and obj.startswith("routerai:"):
        out.append((path, obj))
    return out


def set_value_at_path(obj: Any, path: str, value: Any) -> None:
    """Устанавливает значение по пути вида a.b[0].c (in-place)."""
    parts = path.replace("[", ".[").split(".")
    cur = obj
    for part in parts[:-1]:
        if part.startswith("[") and part.endswith("]"):
            cur = cur[int(part[1:-1])]
        else:
            cur = cur[part]
    last = parts[-1]
    if last.startswith("[") and last.endswith("]"):
        cur[int(last[1:-1])] = value
    else:
        cur[last] = value


# --------------------------------------------------------------------------- #
# Выбор замены для ссылки routerai:<id>
# --------------------------------------------------------------------------- #
def find_replacement(
    model_id: str,
    available: list[dict[str, Any]],
    ref_id: str,
) -> Optional[dict[str, Any]]:
    """Ищет ближайшую дешёвую альтернативу среди доступных моделей.

    `available` — список моделей, среди которых ищем замену (обычно топ-N,
    выбранный `select_top_models`). Приоритет:
      1. Тот же id (уже актуален) — возвращает None (замена не нужна).
      2. Модель того же провайдера (например google/gemini-3.1 -> google/*)
      3. Эталонная модель
      4. Самая дешёвая cheap-модель с tools
    Возвращает запись-кандидата, либо None если замена не найдена.
    """
    by_id = {m["id"]: m for m in available}
    if model_id in by_id:
        return None  # уже актуальна

    provider = model_id.split("/")[0] if "/" in model_id else None

    # 1) Тот же провайдер, cheap-модель с tools
    if provider:
        same_provider = [
            m for m in available
            if m.get("provider") == provider
            and m.get("tier") == "cheap"
            and m.get("supports_tools")
        ]
        same_provider.sort(key=lambda m: m.get("cost_per_turn_estimate_rub") or 0)
        if same_provider:
            return same_provider[0]

    # 2) Эталон
    ref = by_id.get(ref_id)
    if ref:
        return ref

    # 3) Любая самая дешёвая cheap с tools
    cheap_tools = [
        m for m in available
        if m.get("tier") == "cheap" and m.get("supports_tools")
    ]
    cheap_tools.sort(key=lambda m: m.get("cost_per_turn_estimate_rub") or 0)
    if cheap_tools:
        return cheap_tools[0]

    return None


# --------------------------------------------------------------------------- #
# Отбор топ-N моделей (гибридный score из рекомендаций + квоты по тирам)
# --------------------------------------------------------------------------- #
def hybrid_score(
    model_id: str,
    recommendations: Optional[dict[str, Any]],
    weights: dict[str, float],
) -> float:
    """Гибридный score модели: сумма score по категориям рекомендаций с весами.

    Если рекомендаций нет (или модель не попала ни в одну категорию) — 0.
    """
    if not recommendations:
        return 0.0
    total = 0.0
    for cat_id, cat in recommendations.get("categories", {}).items():
        weight = weights.get(cat_id, 0.0)
        if weight <= 0:
            continue
        for m in cat.get("top", []):
            if m.get("id") == model_id:
                total += m.get("score", 0.0) * weight
    return total


def select_top_models(
    catalog: list[dict[str, Any]],
    sync_cfg: dict[str, Any],
    recommendations: Optional[dict[str, Any]],
    ref_id: str,
    ref_cfg: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Выбирает топ-N моделей из каталога для конфигов.

    Логика:
      1. Всегда включается эталон (эффективный reference_model.id) — рабочая лошадка.
      2. Для остальных считается гибридный score: агрегат рекомендаций по
         категориям с весами (см. DEFAULT_SELECTION_WEIGHTS / sync.selection.weights).
         Если рекомендаций нет — фолбэк на (tier, cost) как раньше.
      3. Квоты по тирам (sync.selection.quotas): cheap / medium / premium,
         чтобы во всех ценовых категориях были лучшие модели.
      4. DefaultAgentGate: кандидат на роль default-агента (эталон или модель,
         лучше всех подходящая для default_agent) НЕ может выйти в топ на роль
         default, если он в watchlist и не прошёл гейт надёжности (approved_ids) —
         такой кандидат помечается и на его место добирается следующая модель.
      5. Итоговый список сортируется по релевантности: сначала эталон + топ-cheap
         (рабочие лошадки), затем топ-premium (качественные), medium в конце;
         внутри группы — гибридный score по убыванию.

    Возвращает список записей каталога (без изменения исходного).
    """
    selection = sync_cfg.get("selection") or {}
    total = int(selection.get("total", 0))
    quotas = selection.get("quotas") or {}
    weights = {**DEFAULT_SELECTION_WEIGHTS, **(selection.get("weights") or {})}
    ref_cfg = ref_cfg or {}

    if total <= 0:
        # Отбор отключён — берём всё как раньше (по тиру и цене)
        return sorted(
            catalog,
            key=lambda m: (TIER_ORDER.get(m.get("tier"), 9),
                           m.get("cost_per_turn_estimate_rub") or 0),
        )

    by_id = {m["id"]: m for m in catalog}

    # Эталон всегда первый
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    ref = by_id.get(ref_id)
    if ref:
        result.append(ref)
        seen.add(ref["id"])

    # DefaultAgentGate: модели в watchlist, не прошедшие гейт, не могут
    # попасть в топ на роль default-агента (эталон в отборе остаётся —
    # это рабочая лошадка, но не для роли default).
    gate_blocked: set[str] = {
        m["id"]
        for m in catalog
        if is_watchlisted(m.get("id"), ref_cfg) and not is_model_approved(m.get("id"), ref_cfg)
    }

    # Рекомендации могут отсутствовать — тогда чистая цена/тир
    def fallback_score(m: dict[str, Any]) -> float:
        # чем дешевле и ниже тир — тем лучше (переворачиваем для сортировки desc)
        return 1e9 - (TIER_ORDER.get(m.get("tier"), 9) * 1e6 + (m.get("cost_per_turn_estimate_rub") or 0))

    def score(m: dict[str, Any]) -> float:
        if recommendations:
            return hybrid_score(m["id"], recommendations, weights)
        return fallback_score(m)

    # Кандидаты по каждому тиру, отсортированы по гибридному score desc
    # (при равенстве — по id для детерминированности)
    candidates_by_tier: dict[str, list[dict[str, Any]]] = {"cheap": [], "medium": [], "premium": []}
    for m in catalog:
        tier = m.get("tier")
        if tier in candidates_by_tier:
            candidates_by_tier[tier].append(m)
    for tier in candidates_by_tier:
        candidates_by_tier[tier].sort(key=lambda m: (score(m), m["id"]), reverse=True)

    # Квоты: если квоты не заданы — распределяем пропорционально
    filled = {"cheap": 0, "medium": 0, "premium": 0}
    if not quotas:
        base_share = {t: len(v) for t, v in candidates_by_tier.items()}
        total_available = sum(base_share.values()) or 1
        remaining = total - len(result)
        # минимум по 1 на непустой тир, остальное пропорционально
        for tier in TIER_ORDER:
            if candidates_by_tier[tier] and remaining > 0:
                share = max(1, round(remaining * base_share[tier] / total_available))
                quotas[tier] = min(share, len(candidates_by_tier[tier]))
                remaining -= quotas[tier]
        # докидываем остаток в cheap
        if remaining > 0:
            quotas["cheap"] = quotas.get("cheap", 0) + remaining

    # Заполняем квоты (эталон уже занимает место в своём тире).
    # DefaultAgentGate: модели на роль default_agent (эталон в watchlist /
    # не approved, либо топ-кандидат категории default_agent) пропускаем.
    def gated(m: dict[str, Any]) -> bool:
        """Модель заблокирована для роли default-агента (ожидает подтверждения)."""
        if m["id"] not in gate_blocked:
            return False
        # Гейт касается только роли default-агента: для других ролей
        # (planner/reviewer/...) watchlist-модель не блокируется.
        if recommendations:
            default_top = {
                x.get("id")
                for x in recommendations.get("categories", {}).get("default_agent", {}).get("top", [])
            }
            return m["id"] in default_top
        # Без рекомендаций считаем default-ролью самого эталона (watchlist)
        return m["id"] == ref_id

    for tier in TIER_ORDER:
        quota = int(quotas.get(tier, 0))
        if quota <= 0:
            continue
        already = sum(1 for m in result if m.get("tier") == tier)
        quota -= already  # эталон и прочие уже заняли места
        if quota <= 0:
            continue
        for m in candidates_by_tier[tier]:
            if m["id"] in seen:
                continue
            if gated(m):
                continue  # ждёт подтверждения надёжности — не для default
            result.append(m)
            seen.add(m["id"])
            filled[tier] += 1
            if filled[tier] >= quota:
                break

    # Добираем, если остались места (например квота превысила наличие)
    remaining = total - len(result)
    if remaining > 0:
        for tier in TIER_ORDER:
            for m in candidates_by_tier[tier]:
                if remaining <= 0:
                    break
                if m["id"] in seen:
                    continue
                result.append(m)
                seen.add(m["id"])
                remaining -= 1

    # Финальная сортировка по релевантности:
    #   1) cheap (рабочие лошадки), 2) premium (качественные), 3) medium
    #   внутри группы — гибридный score desc (эталон — первым в cheap)
    def relevance_key(m: dict[str, Any]) -> tuple[int, float, int, str]:
        tier = m.get("tier")
        group = {"cheap": 0, "premium": 1, "medium": 2}.get(tier, 3)
        is_ref = 0 if m["id"] == ref_id else 1
        return (group, -score(m), is_ref, m["id"])

    result.sort(key=relevance_key)
    return result


# --------------------------------------------------------------------------- #
# Человекочитаемое имя модели
# --------------------------------------------------------------------------- #
# Метки ценовых категорий (tier) для отображения в названии модели
TIER_TAGS = {
    "cheap": "cheap",
    "medium": "medium",
    "premium": "premium",
}

# Иконки входных модальностей (помимо text) для бейджа в названии модели
MODALITY_ICONS = {
    "image": "📷",
    "video": "🎥",
    "audio": "🎤",
    "file": "📎",
}


def modality_badge(m: dict[str, Any]) -> str:
    """Бейдж входных модальностей: что модель видит помимо текста.

    Возвращает строку вида «📷🎥» (по одному символу на модальность)
    или пустую строку, если модель принимает только текст.
    """
    ins = m.get("input_modalities") or []
    icons = [MODALITY_ICONS[x] for x in ins if x != "text" and x in MODALITY_ICONS]
    return "".join(icons)


def pretty_model_name(m: dict[str, Any]) -> str:
    """Краткое человекочитаемое имя из каталога (или из id — фолбэк).

    Из каталога `name` обычно уже читаемое («DeepSeek: DeepSeek V4 Flash 0731»,
    «Z.ai: GLM 5.3», «Ling-3.0-flash»). Если пусто — строим из id:
    провайдер + модель без префикса, `:exacto` → « (exacto)».

    Алиасы `~...-latest` помечаются суффиксом « (latest)», чтобы их можно
    было отличить в UI от датированной версии с тем же именем
    (например «DeepSeek V4 Flash 0731» vs «DeepSeek V4 Flash 0731 (latest)»).

    В конец имени добавляется:
      - бейдж модальностей (что видит помимо текста): «📷🎥»;
      - ценовая категория (tier) в квадратных скобках: «[cheap]».
    Например: «Z.ai: GLM 5.3 Flash 📷🎥 [cheap]».
    """
    model_id: str = m.get("id") or ""
    name = (m.get("name") or "").strip()
    if not name:
        if "/" in model_id:
            provider, model = model_id.split("/", 1)
        else:
            provider, model = "", model_id
        # убираем суффикс :exacto и помечаем скобками
        if model.endswith(":exacto"):
            model = model[: -len(":exacto")] + " (exacto)"
        name = f"{provider} {model}".strip()
    # Алиас ~provider/model-latest → помечаем «(latest)».
    # Имя из каталога уже содержит пометку (build_report.normalize_model),
    # поэтому не дублируем, если суффикс уже есть.
    if model_id.startswith("~") and model_id.endswith("-latest"):
        if not name.endswith("(latest)"):
            name = f"{name} (latest)"
    badge = modality_badge(m)
    if badge:
        name = f"{name} {badge}"
    tier = m.get("tier")
    if tier in TIER_TAGS:
        name = f"{name} [{TIER_TAGS[tier]}]"
    return name
def to_ai_core_model(m: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Запись каталога -> формат ai-core llm.providers.routerai.models."""
    # Потолки контекста по тирам (token_limits в filter_config.json)
    max_input, max_output = apply_token_limits(
        m.get("context_length"), m.get("tier")
    )
    return {
        "id": m["id"],
        "name": pretty_model_name(m),
        "url": base_url.rstrip("/"),
        "toolCalling": bool(m.get("supports_tools")),
        "vision": bool(m.get("supports_vision")),
        "maxInputTokens": max_input,
        "maxOutputTokens": max_output,
    }


def to_clm_model(m: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Запись каталога -> формат VS Code chatLanguageModels model (без _meta)."""
    model = to_ai_core_model(m, base_url)
    return model


# --------------------------------------------------------------------------- #
# Обновление ai-core config.json
# --------------------------------------------------------------------------- #
def resolve_asr_alias(
    alias: str,
    asr_catalog: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Находит актуальную ASR-модель по алиасу (или по id).

    Приоритет:
      1. Точное совпадение id (уже актуальна).
      2. Модель того же провайдера (например microsoft/mai-transcribe-2 →
         microsoft/*), cheapest.
      3. Самая дешёвая ASR-модель в каталоге.
    Возвращает запись каталога или None, если каталог пуст.
    """
    by_id = {m["id"]: m for m in asr_catalog}
    if alias in by_id:
        return by_id[alias]

    provider = alias.split("/")[0] if "/" in alias else None
    if provider:
        same_provider = [
            m for m in asr_catalog
            if m.get("provider") == provider and m.get("rub_per_min") is not None
        ]
        same_provider.sort(key=lambda m: m.get("rub_per_min") or 0)
        if same_provider:
            return same_provider[0]

    priced = [m for m in asr_catalog if m.get("rub_per_min") is not None]
    priced.sort(key=lambda m: m.get("rub_per_min") or 0)
    return priced[0] if priced else None


def update_ai_core_asr(
    cfg: dict[str, Any],
    asr_catalog: list[dict[str, Any]],
    asr_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Обновляет секцию asr в config.json ai-core. Возвращает лог изменений.

    Секция asr в config.json:
        {
          "personal_model": "microsoft/mai-transcribe-2",
          "bulk_model": "qwen/qwen3-asr-0.6b",
          "models": [ {id, name, rub_per_min, tier}, ... ]  # актуальный каталог
        }

    personal_model/bulk_model — полные id провайдера (без routerai: префикса,
    как и в llm.providers.routerai.models). Если текущий id отсутствует в
    каталоге — заменяется на ближайшую альтернативу (resolve_asr_alias).
    """
    log: dict[str, Any] = {"personal": None, "bulk": None, "models": {}}
    asr = cfg.setdefault("asr", {})

    personal_aliases = asr_cfg.get("personal_aliases") or []
    bulk_aliases = asr_cfg.get("bulk_aliases") or []

    # Текущие значения (могут отсутствовать — тогда берём первый алиас)
    cur_personal = asr.get("personal_model") or (personal_aliases[0] if personal_aliases else None)
    cur_bulk = asr.get("bulk_model") or (bulk_aliases[0] if bulk_aliases else None)

    def _resolve(cur: Optional[str], aliases: list[str]) -> Optional[dict[str, Any]]:
        if cur:
            repl = resolve_asr_alias(cur, asr_catalog)
            if repl:
                return repl
        for alias in aliases:
            repl = resolve_asr_alias(alias, asr_catalog)
            if repl:
                return repl
        return None

    new_personal = _resolve(cur_personal, personal_aliases)
    new_bulk = _resolve(cur_bulk, bulk_aliases)

    if new_personal:
        if asr.get("personal_model") != new_personal["id"]:
            log["personal"] = {"old": asr.get("personal_model"), "new": new_personal["id"]}
        asr["personal_model"] = new_personal["id"]
    if new_bulk:
        if asr.get("bulk_model") != new_bulk["id"]:
            log["bulk"] = {"old": asr.get("bulk_model"), "new": new_bulk["id"]}
        asr["bulk_model"] = new_bulk["id"]

    # Актуальный список ASR-моделей (для выбора вручную / метрик)
    old_ids = [m.get("id") for m in asr.get("models", [])]
    new_ids = [m["id"] for m in asr_catalog]
    asr["models"] = [
        {
            "id": m["id"],
            "name": m.get("name"),
            "rub_per_min": m.get("rub_per_min"),
            "tier": m.get("tier"),
        }
        for m in asr_catalog
    ]
    log["models"] = {
        "old_count": len(old_ids),
        "new_count": len(new_ids),
        "removed": [i for i in old_ids if i not in set(new_ids)],
        "added": [i for i in new_ids if i not in set(old_ids)],
    }
    return log


def update_ai_core_config(
    cfg: dict[str, Any],
    catalog: list[dict[str, Any]],
    sync_cfg: dict[str, Any],
    ref_id: str,
    recommendations: Optional[dict[str, Any]] = None,
    ref_cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Обновляет models + ссылки в конфиге. Возвращает лог изменений."""
    log: dict[str, Any] = {"models": [], "refs": []}
    routerai = (cfg.get("llm", {}).get("providers", {}) or {}).get("routerai")
    if not routerai:
        return log

    base_url = routerai.get("base_url") or sync_cfg.get("clm_url", "https://routerai.ru/api/v1")

    selected = select_top_models(catalog, sync_cfg, recommendations, ref_id, ref_cfg)

    old_ids = [m.get("id") for m in routerai.get("models", [])]
    new_ids = [m["id"] for m in selected]
    removed = [i for i in old_ids if i not in set(new_ids)]
    added = [i for i in new_ids if i not in set(old_ids)]

    routerai["models"] = [to_ai_core_model(m, base_url) for m in selected]
    log["models"] = {"old_count": len(old_ids), "new_count": len(new_ids),
                     "removed": removed, "added": added}

    # Переписывание ссылок routerai:<id>
    # Актуальными считаются только id из выбранного топ-N (selected),
    # а не из всего каталога — иначе ссылка укажет на модель вне списка.
    if sync_cfg.get("rewrite_refs", True):
        refs = find_model_refs(cfg)
        selected_ids = {m["id"] for m in selected}
        for path, value in refs:
            cur_id = value.split(":", 1)[1]
            if cur_id in selected_ids:
                continue  # актуальна
            repl = find_replacement(cur_id, selected, ref_id)
            if repl is None:
                continue
            new_ref = f"routerai:{repl['id']}"
            set_value_at_path(cfg, path, new_ref)
            log["refs"].append({"path": path, "old": value, "new": new_ref,
                                "reason": f"заменено на альтернативу из топ-N: {repl['id']}"})

    return log


# --------------------------------------------------------------------------- #
# Генерация imports/chatLanguageModels.json
# --------------------------------------------------------------------------- #
def build_clm_provider(
    catalog: list[dict[str, Any]],
    sync_cfg: dict[str, Any],
    recommendations: Optional[dict[str, Any]] = None,
    ref_id: Optional[str] = None,
    ref_cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Формирует провайдера VS Code chatLanguageModels из каталога."""
    if not ref_id:
        # Эффективный эталон: approved или авто-выбор (самый дешёвый
        # cheap+tools вне watchlist). На случай вызова без конфига —
        # безопасный фолбэк без хардкода снапшота.
        resolved_id, _status = effective_reference_id(
            {"reference_model": ref_cfg or {}}, catalog
        )
        ref_id = resolved_id or ""
    selected = select_top_models(catalog, sync_cfg, recommendations, ref_id, ref_cfg)
    base_url = sync_cfg.get("clm_url", "https://routerai.ru/api/v1")
    return {
        "name": sync_cfg.get("clm_provider_name", "routerai"),
        "vendor": sync_cfg.get("clm_vendor", "customendpoint"),
        "apiKey": sync_cfg.get("clm_api_key", "${input:chat.lm.secret.-77660a4d}"),
        "apiType": sync_cfg.get("clm_api_type", "chat-completions"),
        "models": [to_clm_model(m, base_url) for m in selected],
    }


# --------------------------------------------------------------------------- #
# Diff-печать
# --------------------------------------------------------------------------- #
def print_diff(cfg_path: Path, cfg_before: dict[str, Any], cfg_after: dict[str, Any], log: dict[str, Any]) -> None:
    print("=" * 72)
    print("SYNC CONFIGS (dry-run — ничего не записано)" if __dry_run else "SYNC CONFIGS")
    print("=" * 72)
    print(f"Файл: {cfg_path}")

    models_log = log.get("models", {})
    removed = models_log.get("removed", [])
    added = models_log.get("added", [])
    print(f"\nМодели: {models_log.get('old_count')} -> {models_log.get('new_count')}")
    if removed:
        print(f"  Удаляются ({len(removed)}):")
        for i in removed:
            print(f"    - {i}")
    if added:
        print(f"  Добавляются ({len(added)}):")
        for i in added:
            print(f"    + {i}")

    refs = log.get("refs", [])
    if refs:
        print(f"\nСсылки routerai: (замен: {len(refs)})")
        for r in refs:
            print(f"  {r['path']}:")
            print(f"    {r['old']}  ->  {r['new']}   ({r['reason']})")
    else:
        print("\nСсылки routerai: изменений нет")

    asr_log = log.get("asr")
    if asr_log:
        print("\nASR (распознавание речи):")
        for key, label in (("personal", "personal_model"), ("bulk", "bulk_model")):
            entry = asr_log.get(key)
            if entry:
                print(f"  {label}: {entry['old']}  ->  {entry['new']}")
        m = asr_log.get("models", {})
        if m.get("removed"):
            print(f"  Удаляются из списка ({len(m['removed'])}):")
            for i in m["removed"]:
                print(f"    - {i}")
        if m.get("added"):
            print(f"  Добавляются в список ({len(m['added'])}):")
            for i in m["added"]:
                print(f"    + {i}")
        if not asr_log.get("personal") and not asr_log.get("bulk") and not m.get("removed") and not m.get("added"):
            print("  изменений нет")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать дифф без записи (по умолчанию true, отключить нельзя безопасно)")
    parser.add_argument("--apply", action="store_true",
                        help="Реально записать изменения в ai-core config и imports/clm")
    parser.add_argument("--apply-clm", action="store_true",
                        help="Дополнительно обновить источник VS Code chatLanguageModels.json")
    parser.add_argument("--no-config", action="store_true",
                        help="Не обновлять ai-core config.json (только импорт clm)")
    parser.add_argument("--catalog", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        fcfg = load_json(PROJECT_ROOT / "filter_config.json")
    except FileNotFoundError:
        sys.stderr.write("Ошибка: не найден filter_config.json\n")
        return 1

    sync_cfg = fcfg.get("sync", {})
    paths = fcfg["paths"]
    catalog_path = args.catalog or (PROJECT_ROOT / paths["catalog"])
    try:
        catalog = load_catalog(catalog_path)
    except FileNotFoundError:
        sys.stderr.write(f"Ошибка: не найден каталог {catalog_path}. Сначала запустите build_report.py\n")
        return 1

    ref_cfg = fcfg.get("reference_model") or {}
    # Эффективный эталон: если reference_model.id не утверждён (approved_ids),
    # авто-выбор — самая дешёвая cheap-модель с tools вне watchlist.
    ref_id, _ref_status = effective_reference_id(fcfg, catalog)
    if not ref_id:
        sys.stderr.write(
            f"Ошибка: эталон не выбран (status={_ref_status}). "
            "Каталог пуст или все cheap-модели с tools в watchlist.\n"
        )
        return 1

    # Рекомендации для гибридного score (могут отсутствовать — фолбэк на цену)
    recommendations: Optional[dict[str, Any]] = None
    rec_path = PROJECT_ROOT / paths.get("recommendations", "data/recommendations.json")
    if rec_path.exists():
        recommendations = load_json(rec_path)
    else:
        print("[info] recommendations.json не найден — отбор по цене/тиру")

    # ── Защита записи: если каталог не существует, пуст или устарел
    # (pipeline не доработал) — блокируем ВСЮ запись (clm, config.json,
    # clm_source). Без этого таймер мог бы записать битый/пустой конфиг
    # поверх рабочего. В dry-run — только предупреждение (дифф показываем).
    catalog_file = catalog_path
    if args.apply and not check_catalog_ok(catalog_file, verbose=True):
        sys.stderr.write(
            "[sync] Запись ОТМЕНЕНА: каталог моделей недоступен/устарел. "
            "Запустите fetch_models.py && filter_models.py && build_report.py "
            "&& recommend.py перед sync_configs.py --apply\n"
        )
        return 2
    if args.apply:
        print(f"[sync] Каталог актуален (возраст {catalog_age_hours(catalog_file):.1f} ч, "
              f"лимит {stale_after_hours():.0f} ч) — пишу конфиги")
    elif not check_catalog_ok(catalog_file, verbose=False):
        print("[dry-run] ⚠ Каталог устарел или недоступен — при --apply запись "
              "была бы отменена (дифф ниже показан для справки)")

    # 1. Импорт clm (всегда пишется при --apply)
    clm_provider = build_clm_provider(catalog, sync_cfg, recommendations, ref_id, ref_cfg)
    clm_import_path = PROJECT_ROOT / paths["clm_import"]
    if args.apply:
        clm_import_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = clm_import_path.with_suffix(clm_import_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            # Только массив моделей, без обёртки провайдера
            json.dump(clm_provider["models"], f, ensure_ascii=False, indent=2)
        tmp.replace(clm_import_path)
        print(f"[sync] Импорт clm: {clm_import_path} ({len(clm_provider['models'])} моделей, без обёртки)")
    else:
        print(f"[dry-run] Импорт clm: {clm_import_path} будет содержать "
              f"{len(clm_provider['models'])} моделей (массив без обёртки)")

    # 2. Обновление ai-core config.json
    config_log: dict[str, Any] = {"models": {}, "refs": []}
    if not args.no_config:
        global __dry_run
        __dry_run = not args.apply

        cfg_path = ai_core_config_path()
        if not cfg_path.exists():
            sys.stderr.write(f"Ошибка: не найден конфиг ai-core: {cfg_path}\n")
            return 1
        cfg_before = load_json(cfg_path)
        cfg_after = json.loads(json.dumps(cfg_before))  # глубокое копирование
        config_log = update_ai_core_config(cfg_after, catalog, sync_cfg, ref_id, recommendations, ref_cfg)

        # 2a. ASR-секция (audio -> transcription): personal/bulk модели + список
        asr_cat_path = asr_catalog_path()
        asr_log: dict[str, Any] = {}
        if asr_cat_path.exists():
            if args.apply and not check_asr_catalog_ok(asr_cat_path, verbose=True):
                sys.stderr.write(
                    "[sync] ASR-секция НЕ обновлена: ASR-каталог недоступен/устарел. "
                    "Запустите fetch_models.py && filter_models.py && build_report.py\n"
                )
            else:
                asr_catalog = load_catalog(asr_cat_path)
                asr_log = update_ai_core_asr(cfg_after, asr_catalog, fcfg.get("asr", {}))
                config_log["asr"] = asr_log
                if args.apply:
                    print(f"[sync] ASR-секция обновлена ({len(asr_catalog)} моделей в каталоге)")
        else:
            print(f"[sync] ASR-каталог не найден ({asr_cat_path}) — секция asr не обновляется")

        print_diff(cfg_path, cfg_before, cfg_after, config_log)

        if args.apply:
            tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg_after, f, ensure_ascii=False, indent=2)
            tmp.replace(cfg_path)
            print(f"[sync] config.json обновлён: {cfg_path}")
        else:
            print("[dry-run] config.json НЕ изменён. Запустите с --apply для записи.")

    # 3. (опционально) источник VS Code clm
    if args.apply_clm:
        clm_source = Path(sync_cfg.get("clm_source_path",
                          "/home/dev/.vscode-server/data/User/chatLanguageModels.json"))
        if not clm_source.exists():
            sys.stderr.write(f"Ошибка: не найден источник clm: {clm_source}\n")
            return 1
        tmp = clm_source.with_suffix(clm_source.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([clm_provider], f, ensure_ascii=False, indent=2)
        tmp.replace(clm_source)
        print(f"[sync] VS Code clm обновлён: {clm_source}")
    elif args.apply:
        print("[sync] VS Code clm источник НЕ изменён (добавьте --apply-clm)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
