#!/usr/bin/env python3
"""Интеграционные тесты: required_ids / excluded_ids в полном конвейере.

Фаза 5 (регрессия): end-to-end проверка filter_and_categorize +
select_top_models с временным конфигом и минимальным каталогом.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _make_catalog() -> list[dict[str, Any]]:
    """Минимальный каталог (6+ моделей) для интеграционного теста."""
    return [
        {
            "id": "deepseek/deepseek-v4-flash",
            "name": "DeepSeek V4 Flash",
            "architecture": {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": 0.00002, "completion": 0.00006},
            "supported_parameters": ["tools"],
            "created": 1_730_000_000,
        },
        {
            "id": "deepseek/deepseek-v4.1-flash",
            "name": "DeepSeek V4.1 Flash",
            "architecture": {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": 0.000015, "completion": 0.00005},
            "supported_parameters": ["tools"],
            "created": 1_740_000_000,
        },
        {
            "id": "openai/gpt-4o-mini",
            "name": "GPT-4o Mini",
            "architecture": {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": 0.00015, "completion": 0.0006},
            "supported_parameters": ["tools"],
            "created": 1_720_000_000,
        },
        {
            "id": "inclusionai/ling-3.0-flash",
            "name": "Ling 3.0 Flash (excluded)",
            "architecture": {"modality": "text+image->text", "input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "pricing": {"prompt": 0.00001, "completion": 0.00004},
            "supported_parameters": ["tools"],
            "created": 1_750_000_000,
        },
        {
            "id": "anthropic/claude-sonnet-4",
            "name": "Claude Sonnet 4",
            "architecture": {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]},
            "pricing": {"prompt": 0.003, "completion": 0.015},
            "supported_parameters": ["tools", "reasoning"],
            "created": 1_710_000_000,
        },
        {
            "id": "google/gemini-2.0-flash",
            "name": "Gemini 2.0 Flash",
            "architecture": {"modality": "text+image->text", "input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "pricing": {"prompt": 0.00001, "completion": 0.00004},
            "supported_parameters": ["tools"],
            "created": 1_730_000_000,
        },
    ]


def _make_config(tmp_path: Path) -> dict[str, Any]:
    """Временный filter_config.json на основе реального с переопределениями."""
    import filter_models as fm

    cfg = fm.load_config()
    cfg["sync"]["selection"] = {
        "total": 15,
        "required_ids": ["deepseek/deepseek-v4.1-flash"],
        "excluded_ids": ["inclusionai/ling-3.0-flash"],
        "quotas": {"cheap": 10, "medium": 2, "premium": 3},
    }
    cfg["reference_model"] = {
        "id": "deepseek/deepseek-v4-flash",
        "approved_ids": ["deepseek/deepseek-v4-flash"],
        "watchlist": ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash-0731"],
        "fallback": "cheapest_cheap_tools_not_watchlisted",
        "label": "Эталон: DeepSeek V4 Flash",
    }
    # Все пути в tmp_path (чтобы не трогать реальные артефакты)
    for key in list(cfg.get("paths", {})):
        cfg["paths"][key] = str(tmp_path / Path(cfg["paths"][key]).name)
    return cfg


def test_full_pipeline_required_end_to_end(tmp_path: Path) -> None:
    """Полный конвейер: filter_and_categorize + select_top_models с required/excluded.

    Проверяет:
    - оба ID (ref + required) присутствуют в selected
    - excluded модель отсутствует
    - размер ≤ 15
    - эталон на позиции 0
    - effective_reference_id возвращает (v4-flash, approved)
    - required модель НЕ в default_agent.top (если есть рекомендации)
    """
    import filter_models as fm
    import sync_configs as sc
    import reliability as rel

    catalog = _make_catalog()
    cfg = _make_config(tmp_path)

    # Фильтрация
    filtered = fm.filter_and_categorize(catalog, cfg, required_ids=["deepseek/deepseek-v4.1-flash"])
    assert filtered["stats"]["valid"] >= 5  # 5+ моделей прошло
    filtered_ids = {m["id"] for m in
                    filtered["tiers"]["cheap"] + filtered["tiers"]["medium"] + filtered["tiers"]["premium"]}
    assert "deepseek/deepseek-v4-flash" in filtered_ids
    assert "deepseek/deepseek-v4.1-flash" in filtered_ids
    assert "inclusionai/ling-3.0-flash" in filtered_ids  # проходит фильтрацию (valid)

    # Отбор топ-N
    flat = filtered["tiers"]["cheap"] + filtered["tiers"]["medium"] + filtered["tiers"]["premium"]
    sync_cfg = cfg["sync"]
    ref_cfg = cfg["reference_model"]
    selected = sc.select_top_models(
        flat, sync_cfg, recommendations=None,
        ref_id="deepseek/deepseek-v4-flash", ref_cfg=ref_cfg,
    )
    selected_ids = [m["id"] for m in selected]

    # Проверки
    assert selected[0]["id"] == "deepseek/deepseek-v4-flash"  # ref на позиции 0
    assert "deepseek/deepseek-v4.1-flash" in selected_ids  # required в выборке
    assert "inclusionai/ling-3.0-flash" not in selected_ids  # excluded отсутствует
    assert len(selected) <= 15  # total лимит

    # effective_reference_id
    eid, status = rel.effective_reference_id(cfg, catalog)
    assert eid == "deepseek/deepseek-v4-flash"
    assert status == "approved"

    # DefaultAgentGate: required модель в watchlist не approved → gate_blocked,
    # но required bypass-ит gated() — она в выборке, но не default_agent
    # (без рекомендаций проверяем только что она присутствует)
    assert "deepseek/deepseek-v4.1-flash" in selected_ids

    # v4.1-flash и 0731 в watchlist и НЕ approved → обе в gate_blocked.
    # Наличие v4.1-flash в gate_blocked — ключевая семантика плана: он
    # обязателен в выборке (required), но не одобрен для роли default_agent.
    gate_blocked = {
        m["id"]
        for m in catalog
        if rel.is_watchlisted(m["id"], cfg) and not rel.is_model_approved(m["id"], cfg)
    }
    assert "deepseek/deepseek-v4.1-flash" in gate_blocked  # watchlist + not approved
    assert "deepseek/deepseek-v4-flash" not in gate_blocked  # approved — вне гейта

    # Модели помечены supports_tools
    for m in selected:
        assert "supports_tools" in m or "tier" in m
