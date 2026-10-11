#!/usr/bin/env python3
"""Генерация каталога и HTML-отчёта по отфильтрованным моделям routerai.ru.

Читает data/filtered_models.json и формирует:
  - data/catalog.json        — полный нормализованный каталог
  - data/config_models.json  — формат, совместимый с ai-core/config/config.json
  - reports/models_report.html — интерактивный отчёт (поиск, фильтры, топ-10)

Использование:
    python3 build_report.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Ошибка: не установлен модуль jinja2. Выполните: pip install -r requirements.txt\n"
    )
    sys.exit(1)

from ai_core_paths import apply_token_limits  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent
TIER_ORDER = ["cheap", "medium", "premium"]
TIER_LABELS = {
    "cheap": "Cheap · рабочие лошадки агентов",
    "medium": "Medium · без reasoning",
    "premium": "Premium · reasoning / ревью / исследования",
}


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(cfg_path: Optional[Path] = None) -> dict[str, Any]:
    return load_json(cfg_path or (PROJECT_ROOT / "filter_config.json"))


def load_filtered(path: Path) -> dict[str, Any]:
    return load_json(path)


# --------------------------------------------------------------------------- #
# Нормализация
# --------------------------------------------------------------------------- #
def pretty_alias(model_id: str, name: Optional[str]) -> str:
    """Человекочитаемое имя с пометкой алиасов `~...-latest`.

    Алиасы вида `~provider/model-latest` в API routerai.ru дублируют имя
    датированной версии (например `~deepseek/deepseek-v4-flash-latest` →
    «DeepSeek: DeepSeek V4 Flash 0731»). Добавляем суффикс «(latest)», чтобы
    в UI/отчёте алиас отличался от датированной модели. Если пометка уже
    есть в имени — не дублируем.
    """
    name = (name or "").strip() or model_id
    if model_id.startswith("~") and model_id.endswith("-latest") \
            and not name.endswith("(latest)"):
        name = f"{name} (latest)"
    return name


def normalize_model(entry: dict[str, Any]) -> dict[str, Any]:
    """Преобразует запись filtered_models.json в полный каталог-запись.

    Добавляет человекочитаемые поля, форматированные цены, ссылки и т.п.
    """
    pricing = entry.get("pricing_rub_per_1m", {})
    prompt_rub = pricing.get("prompt", 0.0)
    completion_rub = pricing.get("completion", 0.0)
    cost = entry.get("cost_per_turn_estimate_rub", 0.0)

    model_id: str = entry.get("id", "")
    provider = model_id.split("/")[0] if "/" in model_id else "unknown"

    return {
        "id": model_id,
        "name": pretty_alias(model_id, entry.get("name")),
        "provider": provider,
        "context_length": entry.get("context_length"),
        "context_label": format_number(entry.get("context_length")),
        "description_short": entry.get("description_short", ""),
        "has_reasoning": bool(entry.get("has_reasoning")),
        "supports_structured_outputs": bool(entry.get("supports_structured_outputs")),
        "supports_tools": bool(entry.get("supports_tools")),
        "modality": entry.get("modality"),
        "input_modalities": entry.get("input_modalities") or [],
        "output_modalities": entry.get("output_modalities") or [],
        "supports_vision": bool(entry.get("supports_vision")),
        "pricing_rub_per_1m": pricing,
        "prompt_rub": prompt_rub,
        "completion_rub": completion_rub,
        "prompt_label": format_price(prompt_rub),
        "completion_label": format_price(completion_rub),
        "cost_per_turn_estimate_rub": cost,
        "cost_label": format_price(cost),
        "tier": entry.get("tier"),
        "tier_label": entry.get("tier_label"),
        "created": entry.get("created"),
    }


def format_number(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def format_price(value: Optional[float]) -> str:
    """Форматирует цену в рублях: до 1 ₽ — 4 знака, иначе 2."""
    if value is None:
        return "—"
    if value < 1:
        return f"{value:.4f} ₽"
    return f"{value:,.2f} ₽"


# --------------------------------------------------------------------------- #
# Формат ai-core config
# --------------------------------------------------------------------------- #
def build_config_format(
    entries: list[dict[str, Any]],
    base_url: str,
) -> list[dict[str, Any]]:
    """Формирует список моделей в формате ai-core/config/config.json.

    Каждая запись: {id, name, url, toolCalling, vision, maxInputTokens, maxOutputTokens}
    """
    result: list[dict[str, Any]] = []
    for e in sorted(entries, key=lambda x: x["id"]):
        tier = e.get("tier")
        # Потолки контекста по тирам (см. token_limits в filter_config.json):
        # вход ≤ 250K/150K/100K, выход ≤ 16K/8K/8K для cheap/medium/premium.
        max_input, max_output = apply_token_limits(e.get("context_length"), tier)
        ctx = e.get("context_length") or 128_000
        name = pretty_alias(e.get("id", ""), e.get("name")) or e["id"]
        if tier in ("cheap", "medium", "premium"):
            name = f"{name} [{tier}]"
        result.append(
            {
                "id": e["id"],
                "name": name,
                "url": base_url,
                "toolCalling": bool(e.get("supports_tools")),
                "vision": bool(e.get("supports_vision")),
                "maxInputTokens": max_input,
                "maxOutputTokens": max_output,
                "_meta": {
                    "tier": tier,
                    "has_reasoning": bool(e.get("has_reasoning")),
                    "context_length": ctx,
                    "pricing_rub_per_1m": e.get("pricing_rub_per_1m"),
                    "cost_per_turn_estimate_rub": e.get("cost_per_turn_estimate_rub"),
                    "modality": e.get("modality"),
                    "supports_vision": bool(e.get("supports_vision")),
                },
            }
        )
    return result


# --------------------------------------------------------------------------- #
# HTML-отчёт
# --------------------------------------------------------------------------- #
def generate_html_report(
    filtered: dict[str, Any],
    cfg: dict[str, Any],
    template_path: Path,
    out_path: Path,
    recommendations: dict[str, Any] | None = None,
) -> None:
    """Рендерит интерактивный HTML-отчёт по шаблону Jinja2."""
    env = Environment(
        loader=FileSystemLoader(template_path.parent),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)

    tiers: dict[str, list[dict[str, Any]]] = {}
    for t in TIER_ORDER:
        tiers[t] = [normalize_model(e) for e in filtered["tiers"].get(t, [])]

    # Топ-10 cheap по стоимости вызова
    top10 = sorted(tiers["cheap"], key=lambda e: e["cost_per_turn_estimate_rub"])[:10]

    stats = filtered.get("stats", {})
    reference = filtered.get("reference_model")
    if reference:
        reference = normalize_model(reference)

    html = template.render(
        generated_at=filtered.get("generated_at"),
        generated_at_local=format_datetime(filtered.get("generated_at")),
        stats=stats,
        tiers=tiers,
        tier_order=TIER_ORDER,
        tier_labels=TIER_LABELS,
        top10=top10,
        reference=reference,
        call_params=cfg.get("call_params", {}),
        currency=cfg.get("call_params", {}).get("currency", "RUB"),
        format_price=format_price,
        format_number=format_number,
        model_age_days=model_age_days,
        recommendations=recommendations or {},
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    tmp.replace(out_path)


# --------------------------------------------------------------------------- #
# Markdown-отчёт
# --------------------------------------------------------------------------- #
def generate_markdown_report(
    filtered: dict[str, Any],
    cfg: dict[str, Any],
    out_path: Path,
    recommendations: dict[str, Any] | None = None,
) -> None:
    """Генерирует обычный Markdown-отчёт (для чтения без браузера).

    Обладает той же информацией, что HTML: статистика, эталон, рекомендации,
    топ-10 по стоимости, полные таблицы по тирам.
    """
    lines: list[str] = []
    add = lines.append

    stats = filtered.get("stats", {})
    tiers: dict[str, list[dict[str, Any]]] = {}
    for t in TIER_ORDER:
        tiers[t] = [normalize_model(e) for e in filtered["tiers"].get(t, [])]

    top10 = sorted(tiers["cheap"], key=lambda e: e["cost_per_turn_estimate_rub"])[:10]
    reference = filtered.get("reference_model")
    if reference:
        reference = normalize_model(reference)

    generated_at = format_datetime(filtered.get("generated_at"))
    add(f"# ⚡ Каталог моделей routerai.ru")
    add("")
    add(f"Отчёт сгенерирован {generated_at} · Валидных чат-моделей: "
        f"{stats.get('valid', 0)} из {stats.get('total_raw', 0)} в каталоге API")
    add("")

    # Сводка
    add("## 📊 Сводка")
    add("")
    add("| Показатель | Значение |")
    add("|------------|----------|")
    add(f"| Всего в каталоге API | {stats.get('total_raw', '—')} |")
    add(f"| Валидных чат-моделей | {stats.get('valid', '—')} |")
    add(f"| Cheap (рабочие лошадки) | {stats.get('cheap', '—')} |")
    add(f"| Medium (без reasoning) | {stats.get('medium', '—')} |")
    add(f"| Premium (reasoning) | {stats.get('premium', '—')} |")
    excluded = stats.get('excluded_by_modality', 0)
    if excluded:
        add(f"| Исключено (нет text→text) | {excluded} |")
    add("")

    if reference:
        add("## 🎯 Эталон сравнения")
        add("")
        add(f"**{reference['id']}** — prompt {reference['prompt_label']} / "
            f"completion {reference['completion_label']} · типовой шаг агента ≈ "
            f"**{reference['cost_label']}**")
        add("")

    # Рекомендации
    if recommendations:
        cats = recommendations.get("categories", {})
        if cats:
            add("## ★ Рекомендации по задачам (fit-to-purpose)")
            add("")
            rec_meta = recommendations.get("stats", {})
            if rec_meta:
                add(f"> {rec_meta.get('catalog_models', '—')} моделей в каталоге · "
                    f"{rec_meta.get('in_config', '—')} в конфиге ai-core · "
                    f"{rec_meta.get('in_clm', '—')} в clm")
                add("")
            for cat_id, cat in cats.items():
                add(f"### {cat.get('label', cat_id)}")
                add("")
                add(f"_{cat.get('description', '')}_")
                add("")
                top = cat.get("top", [])
                if not top:
                    add("Нет моделей в выборке")
                    add("")
                    continue
                add("| # | Модель | Тир | Score | Стоимость шага | Контекст | В конфиге |")
                add("|---|--------|-----|-------|----------------|----------|-----------|")
                for i, m in enumerate(top[:10], 1):
                    flags = []
                    if m.get("in_config"):
                        flags.append("✅")
                    elif m.get("in_clm"):
                        flags.append("clm")
                    else:
                        flags.append("—")
                    cost = format_price(m.get("cost_per_turn_estimate_rub"))
                    ctx = format_number(m.get("context_length")) if m.get("context_length") else "—"
                    add(f"| {i} | `{m['id']}` | {m.get('tier', '—')} | "
                        f"**{m.get('score', '—')}** | {cost} | {ctx} | {flags[0]} |")
                add("")
            add("---")
            add("")

    # Топ-10
    add("## 🏆 Топ-10 самых дешёвых вызовов (типовой шаг агента)")
    add("")
    if top10:
        add("| # | Модель | Тир | Cost | Context |")
        add("|---|--------|-----|------|---------|")
        for i, m in enumerate(top10, 1):
            add(f"| {i} | `{m['id']}` | {m.get('tier', '—')} | {m['cost_label']} | "
                f"{m.get('context_label', '—')} |")
    else:
        add("Нет данных")
    add("")

    # Полные таблицы по тирам
    for tier in TIER_ORDER:
        items = tiers[tier]
        label = TIER_LABELS.get(tier, tier)
        add(f"## {label} ({len(items)})")
        add("")
        if not items:
            add("Нет моделей")
            add("")
            continue
        add("| Модель | Context | Prompt ₽/1M | Completion ₽/1M | Cost шага | Tools | Vision | Reasoning |")
        add("|--------|---------|-------------|-----------------|-----------|-------|--------|-----------|")
        for m in items:
            name = m.get("name") or m["id"]
            if name != m["id"]:
                cell = f"{name}<br>`{m['id']}`"
            else:
                cell = f"`{m['id']}`"
            add(f"| {cell} | {m.get('context_label', '—')} | {m.get('prompt_label', '—')} | "
                f"{m.get('completion_label', '—')} | {m.get('cost_label', '—')} | "
                f"{'✅' if m.get('supports_tools') else '—'} | "
                f"{'✅' if m.get('supports_vision') else '—'} | "
                f"{'✅' if m.get('has_reasoning') else '—'} |")
        add("")

    # Футер
    add("---")
    add("")
    call_params = cfg.get("call_params", {})
    add(f"Каталог: {stats.get('valid', 0)} чат-моделей (text на входе + text на выходе + tools) · "
        f"Параметры вызова: {call_params.get('prompt_tokens', '—')} prompt / "
        f"{call_params.get('completion_tokens', '—')} completion токенов · "
        f"Цены в {cfg.get('call_params', {}).get('currency', 'RUB')} за 1M токенов · "
        f"Источник: routerai.ru API")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    tmp.replace(out_path)


def format_datetime(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def model_age_days(created: Any) -> int:
    """Возраст модели в днях по unix-timestamp `created` (или -1 если нет)."""
    if not created:
        return -1
    try:
        dt = datetime.fromtimestamp(float(created), tz=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except (ValueError, TypeError, OSError):
        return -1


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filtered",
        type=Path,
        default=None,
        help="Путь к filtered_models.json (по умолчанию из filter_config.json)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Путь к filter_config.json (по умолчанию рядом со скриптом)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        sys.stderr.write(f"Ошибка: не найден конфиг {args.config or 'filter_config.json'}\n")
        return 1

    paths = cfg["paths"]
    filtered_path = args.filtered or (PROJECT_ROOT / paths["filtered"])
    try:
        filtered = load_filtered(filtered_path)
    except FileNotFoundError:
        sys.stderr.write(f"Ошибка: не найден {filtered_path}. Сначала запустите filter_models.py\n")
        return 1

    # Собираем все валидные модели для catalog/config
    all_entries: list[dict[str, Any]] = []
    for t in TIER_ORDER:
        all_entries.extend(filtered["tiers"].get(t, []))

    catalog = [normalize_model(e) for e in all_entries]
    catalog_path = PROJECT_ROOT / paths["catalog"]
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": filtered.get("generated_at"),
                "count": len(catalog),
                "models": catalog,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    base_url = cfg["api"]["base_url"].rstrip("/") + "/"
    config_models = build_config_format(all_entries, base_url)
    config_path = PROJECT_ROOT / paths["config_models"]
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_models, f, ensure_ascii=False, indent=2)

    # ASR-каталог (audio -> transcription): нормализованные записи с ценой ₽/мин
    asr_catalog_path = PROJECT_ROOT / paths["asr_catalog"]
    asr_models_path = PROJECT_ROOT / paths["asr_models"]
    if asr_models_path.exists():
        try:
            asr_data = load_json(asr_models_path)
            asr_entries = asr_data.get("models", [])
            asr_catalog = [
                {
                    "id": e.get("id"),
                    "name": pretty_alias(e.get("id", ""), e.get("name")) or e.get("id"),
                    "provider": (e.get("id") or "").split("/")[0] if "/" in (e.get("id") or "") else "unknown",
                    "description_short": e.get("description_short", ""),
                    "input_modalities": e.get("input_modalities") or [],
                    "output_modalities": e.get("output_modalities") or [],
                    "rub_per_min": e.get("rub_per_min"),
                    "rub_per_min_label": e.get("rub_per_min_label"),
                    "tier": e.get("tier"),
                    "tier_label": e.get("tier_label"),
                    "created": e.get("created"),
                }
                for e in asr_entries
            ]
            asr_catalog.sort(key=lambda x: (x.get("rub_per_min") is None, x.get("rub_per_min") or 0, x["id"]))
            with open(asr_catalog_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "generated_at": asr_data.get("generated_at"),
                        "count": len(asr_catalog),
                        "models": asr_catalog,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            print(f"[report] ASR-каталог:   {asr_catalog_path} ({len(asr_catalog)} моделей)")
        except (json.JSONDecodeError, KeyError) as exc:
            sys.stderr.write(f"[report] Ошибка чтения ASR-моделей {asr_models_path}: {exc}\n")
    else:
        print(f"[report] ASR-модели не найдены ({asr_models_path}) — ASR-каталог пропущен")

    template_path = PROJECT_ROOT / paths["template"]
    report_path = PROJECT_ROOT / paths["report"]
    if not template_path.exists():
        sys.stderr.write(f"Ошибка: шаблон не найден: {template_path}\n")
        return 1

    # Рекомендации (сгенерированы recommend.py).
    # Битый recommendations.json — жёсткая ошибка, а не тихий {}. Причина
    # попадает в отчёт, а не маскируется пустым словарём.
    recommendations: dict[str, Any] = {}
    rec_path = PROJECT_ROOT / paths["recommendations"]
    if rec_path.exists():
        try:
            recommendations = load_json(rec_path)
        except json.JSONDecodeError as exc:
            sys.stderr.write(
                f"[report] Ошибка: {rec_path} повреждён (битый JSON): {exc}\n"
            )
            return 1

    generate_html_report(filtered, cfg, template_path, report_path, recommendations)

    # Markdown-отчёт — та же информация без браузера (reports/models_report.md)
    md_report_path = report_path.with_suffix(".md")
    generate_markdown_report(filtered, cfg, md_report_path, recommendations)

    print(f"[report] Каталог:       {catalog_path} ({len(catalog)} моделей)")
    print(f"[report] ai-core config:{config_path} ({len(config_models)} моделей)")
    print(f"[report] HTML-отчёт:    {report_path}")
    print(f"[report] Markdown-отчёт:{md_report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
