#!/usr/bin/env python3
"""Надёжность эталона: авто-выбор и проверка допустимости.

Фаза 3 (P2: тиры, скоринг, рекомендации, гейт эталона):
  - пока `reference_model.id` не утверждён (не входит в `approved_ids`),
    эталон выбирается автоматически: самая дешёвая (по
    `cost_per_turn_estimate_rub`) cheap-модель с поддержкой tools,
    не входящая в `watchlist`;
  - `approved_ids` — белый список моделей, прошедших гейт надёжности
    (их можно назначать эталоном / default-агентом);
  - `watchlist` — модели, которым нельзя доверять автоматически
    (новые снапшоты, эксперименты) до подтверждения человеком.

`status` результата фиксирует причину выбора:
  - "approved"   — `id` задан в конфиге И входит в `approved_ids`;
  - "auto"       — авто-выбор (самый дешёвый cheap+tools вне watchlist);
  - "no_candidate" — кандидат не найден (каталог пуст / все в watchlist).
"""

from __future__ import annotations

from typing import Any

# Стратегия авто-выбора (значение reference_model.fallback в конфиге)
FALLBACK_CHEAPEST_CHEAP_TOOLS = "cheapest_cheap_tools_not_watchlisted"


def is_model_approved(model_id: Any, cfg: dict[str, Any]) -> bool:
    """Прошла ли модель гейт надёжности (входит в approved_ids)."""
    approved = cfg.get("reference_model", {}).get("approved_ids") or []
    return model_id in approved


def is_watchlisted(model_id: Any, cfg: dict[str, Any]) -> bool:
    """Входит ли модель в watchlist (ожидает подтверждения надёжности)."""
    watchlist = cfg.get("reference_model", {}).get("watchlist") or []
    return model_id in watchlist


def reference_eligibility_notes(cfg: dict[str, Any], catalog: list[dict[str, Any]]) -> list[str]:
    """Человекочитаемые заметки о допустимости моделей в роли эталона.

    Для каждой watchlist-модели каталога и для заданного (но не
    утверждённого) `reference_model.id` объясняет, почему авто-выбор
    обходит их стороной.
    """
    ref_cfg = cfg.get("reference_model") or {}
    notes: list[str] = []

    by_id = {m["id"]: m for m in catalog}

    configured_id = ref_cfg.get("id")
    if configured_id:
        if is_watchlisted(configured_id, cfg):
            notes.append(
                f"{configured_id} в watchlist — не утверждён, авто-выбор вместо него"
            )
        elif configured_id not in (ref_cfg.get("approved_ids") or []):
            notes.append(
                f"{configured_id} не в approved_ids — не утверждён, авто-выбор вместо него"
            )
        elif configured_id not in by_id:
            notes.append(f"{configured_id} отсутствует в каталоге — авто-выбор")

    for wid in ref_cfg.get("watchlist") or []:
        if wid in by_id:
            notes.append(f"{wid} в watchlist — ожидает подтверждения надёжности")

    return notes


def effective_reference_id(
    cfg: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> tuple[Any, str]:
    """Возвращает (id эталона, status) с учётом гейта надёжности.

    Порядок:
      1. Если `reference_model.id` задан, есть в каталоге и входит в
         `approved_ids` — он и есть эталон (status="approved").
      2. Иначе авто-выбор: самая дешёвая (по cost_per_turn_estimate_rub)
         cheap-модель с tools, не входящая в `watchlist`
         (status="auto").
      3. Если кандидатов нет — (None, "no_candidate").
    """
    ref_cfg = cfg.get("reference_model") or {}
    by_id = {m["id"]: m for m in catalog}

    configured_id = ref_cfg.get("id")
    if (
        configured_id
        and configured_id in by_id
        and is_model_approved(configured_id, cfg)
    ):
        return configured_id, "approved"

    # Авто-выбор: cheap + tools + не watchlist, самая дешёвая
    candidates = [
        m
        for m in catalog
        if m.get("tier") == "cheap"
        and m.get("supports_tools")
        and not is_watchlisted(m.get("id"), cfg)
        and m.get("cost_per_turn_estimate_rub") is not None
    ]
    candidates.sort(key=lambda m: (m.get("cost_per_turn_estimate_rub") or 0.0, m["id"]))

    if not candidates:
        return None, "no_candidate"

    return candidates[0]["id"], "auto"


def resolve_reference(
    cfg: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    """Полный резолв эталона для поля reference_resolution в выходах.

    Returns:
        {
          "id": str | None,
          "status": "approved" | "auto" | "no_candidate",
          "approved": bool,       # id в approved_ids
          "fallback": str,        # имя стратегии авто-выбора
          "notes": [str, ...],
        }
    """
    ref_id, status = effective_reference_id(cfg, catalog)
    return {
        "id": ref_id,
        "status": status,
        "approved": is_model_approved(ref_id, cfg) if ref_id else False,
        "fallback": (cfg.get("reference_model") or {}).get(
            "fallback", FALLBACK_CHEAPEST_CHEAP_TOOLS
        ),
        "notes": reference_eligibility_notes(cfg, catalog),
    }
