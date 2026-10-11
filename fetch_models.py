#!/usr/bin/env python3
"""Выгрузка каталога моделей routerai.ru.

Скачивает полный каталог моделей с API routerai.ru и сохраняет:
  - data/routerai_models_raw.json  — сырой ответ API (для диагностики)
  - data/meta.json                 — метаданные выгрузки (дата, кол-во, статус)

Использование:
    python3 fetch_models.py

Параметры берутся из filter_config.json (секция "api" и "paths").
Требования: httpx (см. requirements.txt).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Ошибка: не установлен модуль httpx. Выполните: pip install -r requirements.txt\n"
    )
    sys.exit(1)

# Корень проекта: директория, в которой лежит этот скрипт
PROJECT_ROOT = Path(__file__).resolve().parent


def load_config() -> dict[str, Any]:
    """Загружает filter_config.json и возвращает словарь конфигурации."""
    cfg_path = PROJECT_ROOT / "filter_config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_raw_models(
    base_url: str,
    endpoint: str,
    timeout: float = 30.0,
    user_agent: str = "routerai-models-catalog/1.0",
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Выполняет GET-запрос к каталогу моделей.

    Args:
        base_url: базовый URL API (например, https://routerai.ru/api/v1).
        endpoint: путь эндпоинта (например, /models).
        timeout: таймаут запроса в секундах.
        user_agent: значение заголовка User-Agent.
        api_key: опциональный Bearer-токен (env ROUTERAI_API_KEY).

    Returns:
        Словарь с ответом API (обычно {"data": [...]}).

    Raises:
        httpx.HTTPStatusError: при HTTP-ошибке (4xx/5xx).
        httpx.RequestError: при сетевых проблемах.
    """
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def git_revision() -> Optional[str]:
    """Хеш git-коммита репозитория (или None, если не git)."""
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def save_raw_data(payload: dict[str, Any], raw_path: Path, meta_path: Path) -> dict[str, Any]:
    """Сохраняет сырые данные и метаданные выгрузки на диск.

    Оба файла пишутся атомарно (tmp + replace): при сбое на диске
    существующие файлы остаются прежними.

    Args:
        payload: ответ API (словарь).
        raw_path: путь к файлу сырых данных.
        meta_path: путь к файлу метаданных.

    Returns:
        Словарь метаданных, записанных в meta_path.
    """
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(payload, dict):
        if "data" not in payload:
            raise ValueError("Ответ API не содержит поле 'data'")
        items = payload["data"]
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("Ответ API не содержит список моделей в поле 'data'")

    # Запись raw (атомарно: сначала во временный файл)
    tmp_raw = raw_path.with_suffix(raw_path.suffix + ".tmp")
    with open(tmp_raw, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_raw.replace(raw_path)

    now = datetime.now(timezone.utc)
    meta = {
        "fetched_at": now.isoformat(),
        "fetched_at_unix": int(now.timestamp()),
        "total_models": len(items),
        "endpoint": "models",
        "source": "https://routerai.ru/api/v1/models",
        "api_base_url": "https://routerai.ru/api/v1",
        "git_revision": git_revision(),
        "schema_version": 1,
        "raw_file": str(raw_path),
        "schema_note": "pricing.prompt/completion выражены в рублях за 1 токен",
    }
    # meta — атомарно через tmp + replace (как и raw)
    tmp_meta = meta_path.with_suffix(meta_path.suffix + ".tmp")
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    tmp_meta.replace(meta_path)

    return meta


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "filter_config.json",
        help="Путь к filter_config.json (по умолчанию рядом со скриптом)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только вывести количество моделей, не сохраняя файлы",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config() if args.config == (PROJECT_ROOT / "filter_config.json") else json.loads(args.config.read_text("utf-8"))
    except FileNotFoundError:
        sys.stderr.write(f"Ошибка: не найден конфиг {args.config}\n")
        return 1

    api_cfg = config.get("api", {})
    paths_cfg = config.get("paths", {})
    base_url = os.environ.get("ROUTERAI_API_URL", api_cfg.get("base_url", "https://routerai.ru/api/v1"))
    timeout = float(os.environ.get("ROUTERAI_TIMEOUT", api_cfg.get("timeout_seconds", 30)))
    api_key = os.environ.get("ROUTERAI_API_KEY") or None

    print(f"[fetch] Загрузка каталога: {base_url.rstrip('/')}/{api_cfg.get('models_endpoint', 'models').lstrip('/')}")
    if api_key:
        print("[fetch] Авторизация: Bearer-токен из ROUTERAI_API_KEY")
    try:
        payload = fetch_raw_models(
            base_url=base_url,
            endpoint=api_cfg.get("models_endpoint", "/models"),
            timeout=timeout,
            user_agent=api_cfg.get("user_agent", "routerai-models-catalog/1.0"),
            api_key=api_key,
        )
    except httpx.HTTPStatusError as exc:
        sys.stderr.write(f"[fetch] HTTP-ошибка {exc.response.status_code}: {exc.response.text[:500]}\n")
        return 1
    except httpx.RequestError as exc:
        sys.stderr.write(f"[fetch] Сетевая ошибка: {exc}\n")
        return 1

    items = payload.get("data", []) if isinstance(payload, dict) else payload
    print(f"[fetch] Получено моделей: {len(items)}")

    if args.dry_run:
        print("[fetch] dry-run: файлы не сохранены")
        return 0

    try:
        raw_path = PROJECT_ROOT / paths_cfg.get("raw_data", "data/routerai_models_raw.json")
        meta_path = PROJECT_ROOT / paths_cfg.get("meta", "data/meta.json")
        meta = save_raw_data(payload, raw_path, meta_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"[fetch] Ошибка сохранения: {exc}\n")
        return 1

    print(f"[fetch] Сохранено: {raw_path}")
    print(f"[fetch] Метаданные: {meta_path} (моделей: {meta['total_models']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
