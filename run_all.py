#!/usr/bin/env python3
"""Общий entrypoint конвейера routerai-models: весь функционал в один запуск.

Прогоняет полную цепочку:
    fetch_models.py  →  filter_models.py  →  build_report.py
    →  recommend.py  →  sync_configs.py (dry-run по умолчанию)
    →  compare_with_config.py

Порядок и результаты совпадают с разделом README «Конвейер» и сервисом
systemd/routerai-models-fetch.service.

Использование:
    python3 run_all.py                  # полный прогон, sync в dry-run
    python3 run_all.py --no-fetch       # пропустить сетевую выгрузку (офлайн)
    python3 run_all.py --skip-compare   # без сравнения с ai-core
    python3 run_all.py --apply          # + реальная запись config.json и clm
    python3 run_all.py --apply-clm      # + источник VS Code chatLanguageModels.json
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

STEPS: list[tuple[str, str, list[str]]] = [
    ("fetch_models", "Выгрузка каталога с API", []),
    ("filter_models", "Валидация и тиринг", []),
    ("build_report", "Каталог + конфиг + HTML/MD-отчёт", []),
    ("recommend", "Рекомендации fit-to-purpose", []),
    ("fetch_endpoints", "Endpoint-каталог и route-цены", []),
]

# Шаги конвейера, требующие сетевого доступа (индексы в STEPS).
# --no-fetch пропускает все NETWORK_STEPS, а не только шаг 0.
NETWORK_STEPS = {0, 4}


def run_step(name: str, label: str, extra: list[str]) -> bool:
    """Запускает один шаг конвейера, возвращает успех."""
    print(f"\n── [{name}] {label} " + "─" * max(0, 60 - len(name) - len(label)))
    proc = subprocess.run([PYTHON, str(PROJECT_ROOT / f"{name}.py"), *extra])
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-fetch", action="store_true",
                        help="Пропустить сетевую выгрузку (использовать существующий raw-каталог)")
    parser.add_argument("--skip-compare", action="store_true",
                        help="Не запускать compare_with_config.py в конце")
    parser.add_argument("--apply", action="store_true",
                        help="sync_configs.py --apply: реально записать config.json + импорт clm")
    parser.add_argument("--apply-clm", action="store_true",
                        help="sync_configs.py --apply-clm: + обновить источник VS Code clm")
    args = parser.parse_args(argv)

    steps: list[tuple[str, str, list[str]]] = []
    for idx, step in enumerate(STEPS):
        if idx in NETWORK_STEPS and args.no_fetch:
            name, label, _ = step
            print(f"[run_all] --no-fetch: пропускаем сетевой шаг '{name}'")
            continue
        steps.append(step)

    failures: list[str] = []
    for name, label, extra in steps:
        if not run_step(name, label, extra):
            failures.append(name)
            print(f"[run_all] ❌ Шаг '{name}' завершился с ошибкой, прерываю конвейер\n")
            break

    # Рекомендации нужны build_report/сравнению; safety: не продолжаем после сбоя
    if not failures:
        # 5. sync_configs: dry-run по умолчанию, --apply для записи
        sync_args: list[str] = []
        if args.apply:
            sync_args.append("--apply")
        if args.apply_clm:
            sync_args.append("--apply-clm")
        if not run_step("sync_configs", "Актуализация конфигов ai-core + clm", sync_args):
            failures.append("sync_configs")

    if not failures and not args.skip_compare:
        if not run_step("compare_with_config", "Сравнение с ai-core", []):
            failures.append("compare_with_config")

    if failures:
        print(f"\n[run_all] ⚠ Конвейер завершился с ошибками на шагах: {', '.join(failures)}")
        return 1

    print("\n[run_all] ✅ Конвейер завершён полностью.")
    print("  Отчёт HTML:   reports/models_report.html")
    print("  Отчёт MD:     reports/models_report.md")
    print("  Данные:       data/catalog.json, data/config_models.json, data/recommendations.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
