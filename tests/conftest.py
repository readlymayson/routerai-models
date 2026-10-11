"""Общие фикстуры и хелперы для тестов каталога routerai-models.

Тесты используют настоящие raw-данные (data/routerai_models_raw.json),
когда они доступны, и временные каталоги (tmp_path) для выходных файлов —
чтобы не трогать реальные артефакты и не зависеть от дрейфа счётчиков.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def filter_config() -> dict[str, Any]:
    cfg_path = PROJECT_ROOT / "filter_config.json"
    if not cfg_path.exists():
        pytest.skip("filter_config.json отсутствует")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def raw_models() -> list[dict[str, Any]]:
    """Настоящий срез каталога (или skip, если файла нет)."""
    raw_path = PROJECT_ROOT / "data" / "routerai_models_raw.json"
    if not raw_path.exists():
        pytest.skip("data/routerai_models_raw.json отсутствует")
    with open(raw_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("data", [])


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
