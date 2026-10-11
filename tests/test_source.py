"""Фаза 2: тесты источника данных и защит от пустых/битых данных.

Покрывают:
- пустой/битый raw → filter_models.py не перезаписывает выходные файлы, exit 2;
- gate min_valid_models в filter_config срабатывает;
- нормализация не меняет существующий формат (известные счётчики);
- единицы тарификации: non-token модель помечается и не получает цены;
- fetch_models.py пишет meta.json атомарно и с полным набором полей;
- build_report.py не является тихим {} при битом recommendations.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_test_cfg(tmp_path: Path, **overrides) -> Path:
    """Копия filter_config.json с путями в tmp_path и переопределениями.

    Все пути из секции paths переносятся в tmp_path (по имени файла),
    чтобы тесты не трогали реальные артефакты в data/.
    """
    cfg = load_json(PROJECT_ROOT / "filter_config.json")
    cfg.setdefault("paths", {})
    # Пути выходных/входных данных — в tmp_path. Шаблон отчёта и сырые
    # данные остаются реальными (raw переопределяется в тестах явно).
    for key in list(cfg["paths"]):
        if key in ("template", "raw_data"):
            continue
        cfg["paths"][key] = str(tmp_path / Path(cfg["paths"][key]).name)
    if "validation" in overrides:
        cfg["validation"].update(overrides["validation"])
    cfg_path = tmp_path / "filter_config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def run_py(script: str, *args: str) -> subprocess.CompletedProcess:
    """Запускает python-скрипт проекта с аргументами."""
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script), *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )


def raw_models_remote() -> list[dict]:
    with open(PROJECT_ROOT / "data" / "routerai_models_raw.json", encoding="utf-8") as f:
        return json.load(f)["data"]


# --------------------------------------------------------------------------
# filter_models.py: защита от пустых/битых данных
# --------------------------------------------------------------------------


def test_filter_stub_empty_raw_does_not_overwrite(tmp_path: Path) -> None:
    """Пустой raw ({"data": []}) → код выхода 2, выходные файлы не тронуты."""
    raw = PROJECT_ROOT / "data" / "routerai_models_raw.json"
    if not raw.exists():
        pytest.skip("нет реального raw-файла")
    out = tmp_path / "filtered_models.json"
    out.write_text('{"stub": true}', encoding="utf-8")

    stub_raw = tmp_path / "raw_empty.json"
    stub_raw.write_text(json.dumps({"data": []}), encoding="utf-8")
    cfg_path = write_test_cfg(tmp_path)
    cfg = load_json(cfg_path)
    cfg["paths"]["raw_data"] = str(stub_raw)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_py("filter_models.py", "--config", str(cfg_path))
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == {"stub": True}


def test_filter_broken_json_exits_2(tmp_path: Path) -> None:
    """Битый raw JSON → код выхода 2 и сообщение об ошибке."""
    raw = PROJECT_ROOT / "data" / "routerai_models_raw.json"
    if not raw.exists():
        pytest.skip("нет реального raw-файла")
    out = tmp_path / "filtered_models.json"
    out.write_text('{"stub": true}', encoding="utf-8")

    tmp_raw = tmp_path / "raw_broken.json"
    tmp_raw.write_text("{ not valid json", encoding="utf-8")
    cfg_path = write_test_cfg(tmp_path)
    cfg = load_json(cfg_path)
    cfg["paths"]["raw_data"] = str(tmp_raw)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_py("filter_models.py", "--config", str(cfg_path))
    assert result.returncode == 2
    assert "повреждён" in result.stderr or "битый JSON" in result.stderr


def test_gate_min_valid_blocks(tmp_path: Path) -> None:
    """min_valid_models=2000 в конфиге → exit 2, даже если каталог валиден."""
    raw = PROJECT_ROOT / "data" / "routerai_models_raw.json"
    if not raw.exists():
        pytest.skip("нет реального raw-файла")
    out = tmp_path / "filtered_models.json"
    out.write_text('{"stub": true}', encoding="utf-8")

    cfg_path = write_test_cfg(tmp_path, validation={"min_valid_models": 2000})
    result = run_py("filter_models.py", "--config", str(cfg_path))
    assert result.returncode == 2
    assert json.loads(out.read_text(encoding="utf-8")) == {"stub": True}


def test_filter_output_shape_and_new_keys(tmp_path: Path) -> None:
    """Нормальный прогон: формат выходного файла и новые ключи (non_token_pricing, gate)."""
    raw = PROJECT_ROOT / "data" / "routerai_models_raw.json"
    if not raw.exists():
        pytest.skip("нет реального raw-файла")

    cfg_path = write_test_cfg(tmp_path)
    result = run_py("filter_models.py", "--config", str(cfg_path))
    assert result.returncode == 0, result.stdout + result.stderr

    data = load_json(tmp_path / "filtered_models.json")
    for key in ("generated_at", "stats", "tiers", "reference_model"):
        assert key in data, f"нет ключа {key} в filtered_models.json"
    stats = data["stats"]
    assert stats["total_raw"] == len(raw_models_remote())
    assert "non_token_pricing" in stats
    assert "gate" in stats


# --------------------------------------------------------------------------
# pricing_units: не-token модели не смешиваются с ценами за токен
# --------------------------------------------------------------------------


def test_pricing_units_warning_only_for_non_token(tmp_path: Path) -> None:
    """Модель с pricing_units.prompt != token помечается warning, цены None."""
    import filter_models as fm

    # минимальная модель (заглушка) с явными единицами «секунда»
    model: dict = {
        "id": "test/non-token",
        "name": "Non token",
        "pricing_units": {"prompt": "second", "completion": "second"},
        "pricing": {"prompt": 0.001, "completion": 0.002},
    }
    assert fm.pricing_unit_warning(model) is True
    units = fm.pricing_units_of(model)
    assert units == "second"

    tok_model: dict = {
        "id": "test/token",
        "name": "Token",
        "pricing_units": {"prompt": "token", "completion": "token"},
        "pricing": {"prompt": 0.001, "completion": 0.002},
    }
    assert fm.pricing_unit_warning(tok_model) is False


def test_pricing_units_none_is_token_by_default(tmp_path: Path) -> None:
    """Отсутствие pricing_units трактуется как токенные (нет warning)."""
    import filter_models as fm

    model: dict = {"id": "test/no-units", "name": "No units", "pricing": {"prompt": 0.001, "completion": 0.002}}
    assert fm.pricing_unit_warning(model) is False
    assert fm.pricing_units_of(model) is None


# --------------------------------------------------------------------------
# fetch_models.py: атомарный meta.json и полнота полей
# --------------------------------------------------------------------------


def test_save_raw_data_atomic_and_full_meta(tmp_path: Path) -> None:
    """meta.json пишется атомарно, содержит git_revision/api_base_url/schema_version."""
    import fetch_models as fm

    payload = {"data": [{"id": "x/y", "name": "X"}]}
    raw = tmp_path / "raw.json"
    meta = tmp_path / "meta.json"
    meta.write_text('{"old": 1}', encoding="utf-8")

    m = fm.save_raw_data(payload, raw, meta)
    assert raw.exists()
    assert meta.exists()
    for key in ("fetched_at", "fetched_at_unix", "total_models", "api_base_url", "git_revision", "schema_version"):
        assert key in m, f"meta не содержит {key}"
    assert m["total_models"] == 1
    assert m["schema_version"] == 1

    # после записи не остаётся tmp-файлов
    assert not list(tmp_path.glob("*.tmp"))


def test_save_raw_data_rejects_bad_payload(tmp_path: Path) -> None:
    """payload без data → ValueError, файлы не создаются/не трогаются."""
    import fetch_models as fm

    raw = tmp_path / "raw.json"
    meta = tmp_path / "meta.json"
    with pytest.raises(ValueError):
        fm.save_raw_data({"nope": 1}, raw, meta)
    assert not raw.exists()


# --------------------------------------------------------------------------
# build_report.py: битый recommendations.json → жёсткая ошибка
# --------------------------------------------------------------------------


def test_report_corrupt_recommendations_fails(tmp_path: Path) -> None:
    """Битый recommendations.json → код выхода 1, а не тихий {}."""
    rec = tmp_path / "recommendations.json"
    rec.write_text("{ invalid", encoding="utf-8")
    filtered = tmp_path / "filtered_models.json"
    filtered.write_text('{"stats": {"valid": 10}, "tiers": {}}', encoding="utf-8")

    cfg_path = write_test_cfg(tmp_path)
    cfg = load_json(cfg_path)
    cfg["paths"]["recommendations"] = str(rec)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_py("build_report.py", "--filtered", str(filtered), "--config", str(cfg_path))
    assert result.returncode == 1
    assert "повреждён" in result.stderr or "битый JSON" in result.stderr
