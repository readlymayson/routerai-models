#!/usr/bin/env python3
"""Сравнение моделей ai-core/config/config.json с актуальным каталогом routerai.ru.

Анализирует:
  - Модели из конфига, отсутствующие в каталоге API (устаревшие/удалённые)
  - Новые модели из каталога, которых нет в конфиге (кандидаты на добавление)
  - Расхождения параметров: context_length, toolCalling, цены (для моделей из конфига)

Использование:
    python3 compare_with_config.py [--config PATH] [--catalog PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

from ai_core_paths import (  # noqa: E402
    ai_core_config_path,
    apply_token_limits,
    catalog_path,
    load_filter_config,
)


# --------------------------------------------------------------------------- #
# Загрузка
# --------------------------------------------------------------------------- #
def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_ai_core_config(path: Path) -> dict[str, Any]:
    """Загружает конфигурацию ai-core и возвращает список моделей routerai."""
    cfg = load_json(path)
    providers = (cfg.get("llm") or {}).get("providers") or {}
    routerai = providers.get("routerai") or {}
    models = routerai.get("models") or []
    return {"config_path": path, "models": models}


def load_catalog(path: Path) -> list[dict[str, Any]]:
    """Загружает catalog.json (полный нормализованный каталог)."""
    data = load_json(path)
    return data.get("models", [])


def load_clm_models(path: Path) -> set[str]:
    """Загружает imports/chatLanguageModels.json и возвращает id моделей.

    Поддерживает оба формата:
      - список провайдеров [{name, vendor, models: [...]}]
      - плоский массив моделей [{id, name, ...}]
    Если файл отсутствует — возвращает пустое множество.
    """
    try:
        data = load_json(path)
    except FileNotFoundError:
        return set()
    ids: set[str] = set()
    if isinstance(data, list):
        for prov in data:
            if isinstance(prov, dict) and "models" in prov:
                # формат провайдеров [{... "models": [...]}]
                ids |= {m.get("id") for m in prov["models"]}
            elif isinstance(prov, dict) and "id" in prov:
                # плоский массив моделей [{id, ...}]
                ids.add(prov.get("id"))
    return ids


def load_recommendations(path: Path) -> dict[str, Any]:
    """Загружает data/recommendations.json (если есть), иначе пустой dict."""
    try:
        return load_json(path)
    except FileNotFoundError:
        return {}


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def diff_models(
    config_models: list[dict[str, Any]],
    catalog_models: list[dict[str, Any]],
    clm_ids: set[str] | None = None,
    recommendations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сравнивает модели конфига с каталогом API.

    Args:
        config_models: модели из ai-core config.json
        catalog_models: модели из data/catalog.json
        clm_ids: известные id из VS Code chatLanguageModels.json
        recommendations: data/recommendations.json (скоринг/категории)

    Returns:
        {
          "missing_in_catalog": [...],  # есть в конфиге, нет в API
          "new_in_catalog": [...],      # есть в API, нет в конфиге
          "differences": [...],         # расхождения по моделям из конфига
          "recommendations": {...},     # срез рекомендаций для отчёта
          "clm_ids": [...],             # известные id из VS Code
        }
    """
    clm_ids = clm_ids or set()
    recommendations = recommendations or {}
    cfg_by_id = {m.get("id"): m for m in config_models}
    cat_by_id = {m.get("id"): m for m in catalog_models}

    missing_in_catalog = [m for m in config_models if m.get("id") not in cat_by_id]

    new_in_catalog = [
        m for m in catalog_models
        if m.get("id") not in cfg_by_id and m.get("tier") in ("cheap", "medium")
    ]
    # Сортируем новые: сначала дешёвые
    new_in_catalog.sort(key=lambda m: (m.get("tier") != "cheap", m.get("prompt_rub") or 0))

    differences: list[dict[str, Any]] = []
    for cid, cm in cfg_by_id.items():
        cat = cat_by_id.get(cid)
        if not cat:
            continue  # уже в missing_in_catalog
        diffs = []
        # context_length: конфиг хранит maxInputTokens/maxOutputTokens — уже с
        # потолками по тирам (apply_token_limits). Сравниваем с ожидаемыми
        # значениями, а не с полным context_length каталога, иначе каждый кап
        # выглядел бы как «расхождение».
        exp_in, exp_out = apply_token_limits(cat.get("context_length"), cat.get("tier"))
        cfg_in = cm.get("maxInputTokens")
        cfg_out = cm.get("maxOutputTokens")
        if cfg_in is not None and cfg_in != exp_in:
            diffs.append({
                "field": "maxInputTokens", "config": cfg_in, "catalog": exp_in,
                "note": "ожидаемый потолок по тиру (apply_token_limits)",
            })
        if cfg_out is not None and cfg_out != exp_out:
            diffs.append({
                "field": "maxOutputTokens", "config": cfg_out, "catalog": exp_out,
                "note": "ожидаемый потолок по тиру (apply_token_limits)",
            })
        # toolCalling
        cfg_tools = bool(cm.get("toolCalling"))
        cat_tools = bool(cat.get("supports_tools"))
        if cfg_tools != cat_tools:
            diffs.append({"field": "toolCalling", "config": cfg_tools, "catalog": cat_tools})
        # цены
        pricing = cat.get("pricing_rub_per_1m") or {}
        if pricing:
            diffs.append({
                "field": "pricing_rub_per_1m",
                "config": None,
                "catalog": pricing,
                "note": "в конфиге ai-core цены не хранятся — только в каталоге",
            })
        if diffs:
            differences.append({"id": cid, "diffs": diffs})

    # Срез рекомендаций: топ-5 по каждой категории, помечая наличие в конфиге/clm
    rec_summary: dict[str, Any] = {}
    for cat_id, cat in (recommendations.get("categories") or {}).items():
        top = []
        for m in cat.get("top", [])[:5]:
            top.append({
                "id": m.get("id"),
                "score": m.get("score"),
                "cost_per_turn_estimate_rub": m.get("cost_per_turn_estimate_rub"),
                "tier": m.get("tier"),
                "has_reasoning": m.get("has_reasoning"),
                "in_config": m.get("in_config"),
                "in_clm": m.get("in_clm"),
                "novelty_bonus": m.get("components", {}).get("novelty_bonus"),
            })
        rec_summary[cat_id] = {"label": cat.get("label"), "top": top}

    return {
        "missing_in_catalog": missing_in_catalog,
        "new_in_catalog": new_in_catalog,
        "differences": differences,
        "recommendations": rec_summary,
        "clm_ids": sorted(clm_ids),
        "stats": {
            "config_models": len(config_models),
            "catalog_models": len(catalog_models),
            "matched": len(cfg_by_id) - len(missing_in_catalog),
            "missing": len(missing_in_catalog),
            "new": len(new_in_catalog),
            "clm_models": len(clm_ids),
        },
    }


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #
def print_diff_report(result: dict[str, Any]) -> None:
    """Выводит детальный отчёт сравнения в консоль."""
    stats = result["stats"]
    print("=" * 72)
    print("СРАВНЕНИЕ: ai-core/config/config.json vs каталог routerai.ru")
    print("=" * 72)
    print(f"Моделей в конфиге ai-core: {stats['config_models']}")
    print(f"Моделей в каталоге API:    {stats['catalog_models']}")
    print(f"Совпадает:                 {stats['matched']}")
    print(f"Устаревших (нет в API):    {stats['missing']}")
    print(f"Новых (нет в конфиге):     {stats['new']}")
    print()

    if result["missing_in_catalog"]:
        print("⚠ УСТАРЕВШИЕ МОДЕЛИ (есть в конфиге, нет в каталоге API):")
        for m in result["missing_in_catalog"]:
            print(f"  - {m.get('id')}")
        print()

    if result["new_in_catalog"]:
        print("✦ НОВЫЕ МОДЕЛИ (есть в API, нет в конфиге) — кандидаты:")
        for m in result["new_in_catalog"]:
            tier = m.get("tier", "?")
            prompt = m.get("prompt_rub")
            completion = m.get("completion_rub")
            ctx = m.get("context_length")
            reasoning = " 🧠" if m.get("has_reasoning") else ""
            print(f"  [{tier:>7}] {m.get('id'):<45} prompt={prompt}₽/1M completion={completion}₽/1M ctx={ctx}{reasoning}")
        print()

    if result["differences"]:
        print("△ РАСХОЖДЕНИЯ ПАРАМЕТРОВ (модели из конфига):")
        for item in result["differences"]:
            print(f"  • {item['id']}:")
            for d in item["diffs"]:
                if d["field"] == "pricing_rub_per_1m":
                    p = d["catalog"]
                    print(f"      цена: prompt={p.get('prompt')}₽/1M, completion={p.get('completion')}₽/1M")
                else:
                    print(f"      {d['field']}: конфиг={d['config']} → каталог={d['catalog']}")
        print()

    if result["differences"]:
        print("△ РАСХОЖДЕНИЯ ПАРАМЕТРОВ (модели из конфига):")
        for item in result["differences"]:
            print(f"  • {item['id']}:")
            for d in item["diffs"]:
                if d["field"] == "pricing_rub_per_1m":
                    p = d["catalog"]
                    print(f"      цена: prompt={p.get('prompt')}₽/1M, completion={p.get('completion')}₽/1M")
                else:
                    print(f"      {d['field']}: конфиг={d['config']} → каталог={d['catalog']}")
        print()

    if result.get("recommendations"):
        print("★ РЕКОМЕНДАЦИИ (fit-to-purpose, топ-5 по категориям):")
        for cat_id, cat in result["recommendations"].items():
            print(f"  ◆ {cat['label']}:")
            for m in cat["top"]:
                flags = []
                if m.get("in_config"):
                    flags.append("конфиг")
                if m.get("in_clm"):
                    flags.append("clm")
                if m.get("has_reasoning"):
                    flags.append("🧠")
                if m.get("novelty_bonus"):
                    flags.append("🆕")
                extra = f" [{'/'.join(flags)}]" if flags else ""
                print(f"     {m['id']:<45} score={m.get('score')} cost={m.get('cost_per_turn_estimate_rub')}₽{extra}")
        print()

    clm_n = stats.get("clm_models", 0)
    print(f"VS Code clm знает моделей: {clm_n}")
    print("СОВЕТ: новые дешёвые модели можно добавить в секцию "
          "llm.providers.routerai.models конфига ai-core вручную, "
          "или запустить python3 sync_configs.py --apply для автоматической актуализации.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Путь к ai-core/config/config.json",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Путь к data/catalog.json (по умолчанию из filter_config.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести результат в JSON вместо текстового отчёта",
    )
    args = parser.parse_args(argv)

    try:
        fcfg = load_json(PROJECT_ROOT / "filter_config.json")
    except FileNotFoundError:
        sys.stderr.write("Ошибка: не найден filter_config.json\n")
        return 1

    config_path = args.config or ai_core_config_path()
    catalog_path_resolved = args.catalog or catalog_path()

    try:
        ai_core = load_ai_core_config(config_path)
    except FileNotFoundError:
        sys.stderr.write(f"Ошибка: не найден конфиг ai-core: {config_path}\n")
        return 1

    try:
        catalog = load_catalog(catalog_path_resolved)
    except FileNotFoundError:
        sys.stderr.write(
            f"Ошибка: не найден каталог {catalog_path_resolved}. Сначала запустите build_report.py\n"
        )
        return 1

    clm_ids = load_clm_models(PROJECT_ROOT / fcfg["paths"]["clm_import"])
    recommendations = load_recommendations(PROJECT_ROOT / fcfg["paths"]["recommendations"])

    result = diff_models(ai_core["models"], catalog, clm_ids, recommendations)
    result["config_path"] = str(config_path)
    result["catalog_path"] = str(catalog_path_resolved)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_diff_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
