"""Фаза 3: тиры, скоринг, рекомендации и гейт эталона (reliability).

Покрывают:
- границы тиров из конфига (99.9 -> cheap, 100.0 -> НЕ cheap;
  require_reasoning управляет premium);
- tier_boundaries в выходе filter_and_categorize и непересечение тиров;
- дефицит категорий: select_top_models при нехватке кандидатов
  не плодит фиктивные записи (len(result) <= total);
- потолок новизны: novelty_bonus не перевешивает базу
  (<= base * novelty_ceiling_ratio);
- DefaultAgentGate: watchlist-модель (идёт в топ default_agent) не
  попадает в выбор на роль default;
- авто-выбор эталона: effective_reference_id выбирает НЕ watchlist,
  approved-id не перекрывается watchlist.

Все юнит-тесты работают без сети и не трогают реальные артефакты data/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def base_cfg() -> dict[str, Any]:
    """Копия реального filter_config.json (для assign_tier/filter)."""
    cfg = load_json(PROJECT_ROOT / "filter_config.json")
    return json.loads(json.dumps(cfg))


def mk_model(**over: Any) -> dict[str, Any]:
    """Валидная chat-модель (текст + tools + цены) с переопределениями."""
    m: dict[str, Any] = {
        "id": "test/model",
        "name": "Test Model",
        "architecture": {"modality": "text->text"},
        "pricing": {"prompt": 0.00002, "completion": 0.00006},
        "supported_parameters": ["tools"],
        "created": 1_730_000_000,
    }
    m.update(over)
    return m


# ---------------------------------------------------------------------------
# Тиры: границы 99.9/100.0 и require_reasoning
# ---------------------------------------------------------------------------


def test_tier_boundary_cheap_99p9(tmp_path: Path) -> None:
    """99.9 ₽/1M (prompt и completion) — ещё cheap."""
    import filter_models as fm

    cfg = base_cfg()
    model = mk_model(pricing={"prompt": 0.0000999, "completion": 0.0000999})
    assert fm.assign_tier(model, cfg) == "cheap"


def test_tier_boundary_100_not_cheap(tmp_path: Path) -> None:
    """100.0 ₽/1M — уже НЕ cheap (граница включительно)."""
    import filter_models as fm

    cfg = base_cfg()
    model = mk_model(pricing={"prompt": 0.0001, "completion": 0.0001})
    assert fm.assign_tier(model, cfg) != "cheap"


def test_tier_require_reasoning_for_premium(tmp_path: Path) -> None:
    """require_reasoning=True в конфиге: дорогая модель без reasoning — medium."""
    import filter_models as fm

    cfg = base_cfg()
    model = mk_model(
        pricing={"prompt": 0.001, "completion": 0.002},
        supported_parameters=["tools"],
    )
    # premium требует reasoning — а его нет
    assert fm.assign_tier(model, cfg) == "medium"

    # та же модель с reasoning — premium
    model_r = mk_model(
        pricing={"prompt": 0.001, "completion": 0.002},
        supported_parameters=["tools", "reasoning"],
    )
    assert fm.assign_tier(model_r, cfg) == "premium"


def test_tier_require_reasoning_false_allows_premium(tmp_path: Path) -> None:
    """require_reasoning=False: дорогая модель без reasoning может стать premium."""
    import filter_models as fm

    cfg = base_cfg()
    cfg["tiers"]["premium"]["require_reasoning"] = False
    model = mk_model(
        pricing={"prompt": 0.001, "completion": 0.002},
        supported_parameters=["tools"],
    )
    assert fm.assign_tier(model, cfg) == "premium"


# ---------------------------------------------------------------------------
# filter_and_categorize: tier_boundaries в выходе, непересечение, дефицит
# ---------------------------------------------------------------------------


def test_filter_tier_boundaries_in_output(tmp_path: Path) -> None:
    """tier_boundaries в выходе содержит только числовые параметры (без description)."""
    import filter_models as fm

    cfg = base_cfg()
    models = [
        mk_model(
            id="test/cheap-1",
            pricing={"prompt": 0.00002, "completion": 0.00005},
            supported_parameters=["tools"],
        ),
        mk_model(
            id="test/premium-1",
            pricing={"prompt": 0.001, "completion": 0.002},
            supported_parameters=["tools", "reasoning"],
        ),
    ]
    result = fm.filter_and_categorize(models, cfg)

    tb = result["tier_boundaries"]
    assert "cheap" in tb and "medium" in tb and "premium" in tb
    # границы читаются из конфига, description выбрасывается
    assert tb["cheap"]["max_prompt_rub_per_1m"] == 100.0
    assert "description" not in tb["premium"]
    assert tb["premium"]["min_prompt_rub_per_1m"] == 100.0
    assert tb["premium"]["require_reasoning"] is True

    # распределение: модели попали ровно в один тир каждый
    stats = result["stats"]
    assert stats["cheap"] == 1 and stats["premium"] == 1 and stats["medium"] == 0
    all_ids = [e["id"] for t in ("cheap", "medium", "premium") for e in result["tiers"][t]]
    assert sorted(all_ids) == ["test/cheap-1", "test/premium-1"]


def test_select_no_fake_entries_when_shortage(tmp_path: Path) -> None:
    """Дефицит кандидатов: select_top_models не создаёт фиктивных записей.

    2 модели при total=5 — результат 2 (плюс эталон, если отдельный),
    никаких заглушек сверх каталога.
    """
    import sync_configs as sc

    catalog = [
        {
            "id": "test/a",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 1.0,
        },
        {
            "id": "test/b",
            "tier": "premium",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 50.0,
        },
    ]
    sync_cfg = {
        "selection": {
            "total": 5,
            "quotas": {"cheap": 3, "premium": 3},
        }
    }
    selected = sc.select_top_models(catalog, sync_cfg, None, "test/a", {})
    # не больше, чем реальных моделей в каталоге (все в результате)
    assert len(selected) <= len(catalog)
    ids = {m["id"] for m in selected}
    assert ids == {"test/a", "test/b"}


# ---------------------------------------------------------------------------
# Скоринг: новизна не перевешивает базу
# ---------------------------------------------------------------------------


def test_novelty_ceiling_caps_bonus(tmp_path: Path) -> None:
    """novelty_bonus не превышает base * novelty_ceiling_ratio (0.2)."""
    import recommend as rec

    scoring = base_cfg()["scoring"]
    base = float(scoring.get("base_score", 50))
    ceiling_ratio = float(scoring.get("novelty_ceiling_ratio", 0.2))

    # очень свежая модель (unix-время сейчас) — даст максимальную новизну
    fresh: dict[str, Any] = {
        "id": "test/fresh",
        "name": "Fresh",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "cost_per_turn_estimate_rub": 1.0,
        "context_length": 128_000,
        "supports_tools": True,
        "supports_structured_outputs": False,
        "has_reasoning": False,
    }
    ref: dict[str, Any] = {
        "id": "test/ref",
        "cost_per_turn_estimate_rub": 1.0,
        "context_length": 128_000,
    }
    cat_def = {"weight_cost": 1.0, "weight_quality": 1.0, "coding_bonus": 0}
    s = rec.score_model(fresh, scoring, cat_def, ref, {})
    max_novelty = float(scoring.get("max_novelty_bonus", 8))
    # даже если max_novelty_bonus>ceiling — потолок сработал
    assert s["components"]["novelty_bonus"] <= base * ceiling_ratio
    assert s["components"]["novelty_bonus"] <= max_novelty
    # без потолка бонус был бы больше ceiling — проверяем что ceiling реально режет
    assert s["components"]["novelty_bonus"] <= 10.0


def test_novelty_ceiling_does_not_break_normal(tmp_path: Path) -> None:
    """Обычный случай: score модели с умеренной новизной не ломается."""
    import recommend as rec

    scoring = base_cfg()["scoring"]
    model: dict[str, Any] = {
        "id": "test/stable",
        "name": "Stable",
        "created": 1_720_000_000,  # ~2 года назад — новизна 0
        "cost_per_turn_estimate_rub": 1.0,
        "context_length": 64_000,
        "supports_tools": True,
        "supports_structured_outputs": False,
        "has_reasoning": False,
    }
    ref: dict[str, Any] = {
        "id": "test/ref",
        "cost_per_turn_estimate_rub": 1.0,
        "context_length": 64_000,
    }
    cat_def = {"weight_cost": 1.0, "weight_quality": 1.0, "coding_bonus": 0}
    s = rec.score_model(model, scoring, cat_def, ref, {})
    assert s["score"] > 0
    assert s["components"]["novelty_bonus"] == 0.0


# ---------------------------------------------------------------------------
# Эталон: гейт watchlist/approved и авто-выбор
# ---------------------------------------------------------------------------


def test_effective_reference_auto_picks_cheapest_not_watchlisted(tmp_path: Path) -> None:
    """Авто-выбор: самый дешёвый cheap+tools вне watchlist."""
    import reliability as rel

    catalog = [
        {
            "id": "a/deepseek-new",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.5,
        },
        {
            "id": "b/cheapest",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.3,
        },
        {
            "id": "c/premium",
            "tier": "premium",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 10.0,
        },
    ]
    cfg = {
        "reference_model": {
            "id": None,
            "approved_ids": [],
            "watchlist": ["a/deepseek-new"],
            "fallback": rel.FALLBACK_CHEAPEST_CHEAP_TOOLS,
        }
    }
    ref_id, status = rel.effective_reference_id(cfg, catalog)
    assert status == "auto"
    assert ref_id == "b/cheapest"  # не watchlist, самый дешёвый cheap+tools


def test_effective_reference_approved_wins(tmp_path: Path) -> None:
    """Заданный id в approved_ids — эталон, даже если он в watchlist."""
    import reliability as rel

    catalog = [
        {
            "id": "a/approved-v4",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 1.0,
        },
        {
            "id": "b/cheapest",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.3,
        },
    ]
    cfg = {
        "reference_model": {
            "id": "a/approved-v4",
            "approved_ids": ["a/approved-v4"],
            "watchlist": ["a/approved-v4"],
            "fallback": rel.FALLBACK_CHEAPEST_CHEAP_TOOLS,
        }
    }
    ref_id, status = rel.effective_reference_id(cfg, catalog)
    # approved перекрывает watchlist — эталон уверенно выбран
    assert (ref_id, status) == ("a/approved-v4", "approved")


def test_effective_reference_no_candidate(tmp_path: Path) -> None:
    """Все cheap-кандидаты в watchlist → (None, "no_candidate")."""
    import reliability as rel

    catalog = [
        {
            "id": "a/watched",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.5,
        },
    ]
    cfg = {
        "reference_model": {
            "id": None,
            "approved_ids": [],
            "watchlist": ["a/watched"],
            "fallback": rel.FALLBACK_CHEAPEST_CHEAP_TOOLS,
        }
    }
    ref_id, status = rel.effective_reference_id(cfg, catalog)
    assert ref_id is None and status == "no_candidate"


def test_default_gate_blocks_watchlist_for_default_role(tmp_path: Path) -> None:
    """Watchlist-модель в топе default_agent не выходит на роль default.

    DefaultAgentGate: рекомендации ставят watchlist-модель первой в
    default_agent, но она НЕ утверждена — select_top_models пропускает
    её и место занимает следующая модель. Эталон (авто-выбор, не
    watchlist) остаётся рабочей лошадкой.
    """
    import sync_configs as sc
    import reliability as rel

    ref_cfg = {
        "reference_model": {
            "id": None,
            "approved_ids": [],
            "watchlist": ["a/watched-default"],
            "fallback": rel.FALLBACK_CHEAPEST_CHEAP_TOOLS,
        }
    }
    catalog = [
        {
            "id": "a/watched-default",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.1,
        },
        {
            "id": "b/backup",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.2,
        },
        {
            "id": "c/third",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.3,
        },
    ]
    # рекомендации: watchlist-модель — топ default_agent
    recommendations = {
        "categories": {
            "default_agent": {"top": [{"id": "a/watched-default", "score": 90.0}]}
        }
    }
    sync_cfg = {
        "selection": {
            "total": 2,
            "quotas": {"cheap": 2},
        }
    }
    # ref_id — авто-выбор (не watchlist): b/backup
    selected = sc.select_top_models(catalog, sync_cfg, recommendations, "b/backup", ref_cfg)
    ids = [m["id"] for m in selected]
    # watchlist-модель НЕ входит в топ на роль default: гейт сработал,
    # эталон b/backup + замена c/third заполнили оба места
    assert "a/watched-default" not in ids
    assert "b/backup" in ids
    assert "c/third" in ids


def test_default_gate_allows_approved(tmp_path: Path) -> None:
    """Гейт не трогает approved: watchlist+approved модель проходит в топ."""
    import sync_configs as sc
    import reliability as rel

    ref_cfg = {
        "reference_model": {
            "id": "a/approved",
            "approved_ids": ["a/approved"],
            "watchlist": ["a/approved"],
            "fallback": rel.FALLBACK_CHEAPEST_CHEAP_TOOLS,
        }
    }
    catalog = [
        {
            "id": "a/approved",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.1,
        },
        {
            "id": "b/other",
            "tier": "cheap",
            "supports_tools": True,
            "cost_per_turn_estimate_rub": 0.2,
        },
    ]
    recommendations = {
        "categories": {
            "default_agent": {"top": [{"id": "a/approved", "score": 90.0}]}
        }
    }
    sync_cfg = {
        "selection": {
            "total": 3,
            "quotas": {"cheap": 3},
        }
    }
    selected = sc.select_top_models(catalog, sync_cfg, recommendations, "a/approved", ref_cfg)
    ids = [m["id"] for m in selected]
    assert "a/approved" in ids  # approved — проходит гейт


# ---------------------------------------------------------------------------
# Required / Excluded IDs (Phase 4-5)
# ---------------------------------------------------------------------------


def test_required_ids_included() -> None:
    """Required IDs включаются в результат перед квотами."""
    import sync_configs as sc

    catalog = [
        {"id": "a/ref", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.1},
        {"id": "b/required", "tier": "medium", "supports_tools": True, "cost_per_turn_estimate_rub": 0.5},
        {"id": "c/fill", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.2},
    ]
    sync_cfg = {"selection": {"total": 3, "quotas": {"cheap": 2, "medium": 2, "premium": 0},
                              "required_ids": ["b/required"]}}
    selected = sc.select_top_models(catalog, sync_cfg, None, "a/ref")
    ids = [m["id"] for m in selected]
    assert "b/required" in ids
    # ref на позиции 0
    assert ids[0] == "a/ref"


def test_required_ids_dedup_with_ref() -> None:
    """required_ids содержит ref_id — дубликатов нет."""
    import sync_configs as sc

    catalog = [
        {"id": "a/ref", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.1},
        {"id": "c/fill", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.2},
    ]
    sync_cfg = {"selection": {"total": 2, "quotas": {"cheap": 2},
                              "required_ids": ["a/ref"]}}
    selected = sc.select_top_models(catalog, sync_cfg, None, "a/ref")
    ids = [m["id"] for m in selected]
    # ref counted once
    assert ids.count("a/ref") == 1
    assert len(ids) == 2


def test_required_ids_missing_from_catalog_raises() -> None:
    """required_id отсутствует в by_id → RuntimeError."""
    import sync_configs as sc

    catalog = [
        {"id": "a/ref", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.1},
    ]
    sync_cfg = {"selection": {"total": 2, "quotas": {"cheap": 2},
                              "required_ids": ["b/ghost"]}}
    with pytest.raises(RuntimeError, match="Required model b/ghost missing"):
        sc.select_top_models(catalog, sync_cfg, None, "a/ref")


def test_excluded_ids_filtered_from_quota_fill() -> None:
    """excluded_ids модель не попадает в quota-fill. Ref и required не трогаются."""
    import sync_configs as sc

    catalog = [
        {"id": "a/ref", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.1},
        {"id": "b/excluded", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.15},
        {"id": "c/fill", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.2},
    ]
    sync_cfg = {"selection": {"total": 2, "quotas": {"cheap": 2},
                              "excluded_ids": ["b/excluded"]}}
    selected = sc.select_top_models(catalog, sync_cfg, None, "a/ref")
    ids = [m["id"] for m in selected]
    assert "b/excluded" not in ids
    assert "a/ref" in ids
    assert "c/fill" in ids


def test_total_limit_respected_with_required() -> None:
    """total=15, required=1 → len(result) ≤ 15."""
    import sync_configs as sc

    catalog = [
        {"id": "a/ref", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.1},
        {"id": "b/required", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.15},
    ]
    for i in range(20):
        catalog.append({"id": f"c/fill-{i}", "tier": "cheap", "supports_tools": True,
                        "cost_per_turn_estimate_rub": 0.2 + i * 0.01})
    sync_cfg = {"selection": {"total": 15, "quotas": {"cheap": 15},
                              "required_ids": ["b/required"]}}
    selected = sc.select_top_models(catalog, sync_cfg, None, "a/ref")
    assert len(selected) <= 15


def test_required_excluded_intersection_error() -> None:
    """validate_sync_config с пересечением required ∩ excluded → ошибка."""
    import sync_configs as sc

    cfg = {
        "sync": {"selection": {"required_ids": ["a/x"], "excluded_ids": ["a/x"]}},
    }
    errs = sc.validate_sync_config(cfg)
    assert any("Пересечение" in e and "a/x" in e for e in errs)


def test_reference_position_0_after_sort() -> None:
    """После relevance sort эталон на позиции 0."""
    import sync_configs as sc

    catalog = [
        {"id": "x/cheaper", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.01},
        {"id": "a/ref", "tier": "cheap", "supports_tools": True, "cost_per_turn_estimate_rub": 0.1},
    ]
    sync_cfg = {"selection": {"total": 2, "quotas": {"cheap": 2}}}
    selected = sc.select_top_models(catalog, sync_cfg, None, "a/ref")
    assert selected[0]["id"] == "a/ref"


def test_validate_sync_config_type_checks() -> None:
    """required_ids не список или с int → ошибка."""
    import sync_configs as sc

    errs1 = sc.validate_sync_config({"sync": {"selection": {"required_ids": "not-a-list"}}})
    assert any("required_ids" in e and "list" in e for e in errs1)

    errs2 = sc.validate_sync_config({"sync": {"selection": {"required_ids": ["ok", 42]}}})
    assert any("должны быть строками" in e for e in errs2)


def test_validate_sync_config_duplicates_in_required() -> None:
    """required_ids: ["a", "a"] → ошибка дубликатов."""
    import sync_configs as sc

    errs = sc.validate_sync_config({"sync": {"selection": {"required_ids": ["a", "a"]}}})
    assert any("дубликат" in e for e in errs)


def test_validate_sync_config_quotas_exceed_total() -> None:
    """quotas сумма > total → ошибка."""
    import sync_configs as sc

    errs = sc.validate_sync_config({"sync": {"selection": {"total": 5, "quotas": {"cheap": 3, "medium": 3}}}})
    assert any("превышает" in e for e in errs)


def test_required_ids_filtered_out_diagnostics() -> None:
    """Required модель не прошла фильтрацию → ValueError с причинами."""
    import filter_models as fm

    cfg = base_cfg()
    cfg["sync"] = {"selection": {"required_ids": ["test/no-tools"]}}
    # модель без tools
    models = [mk_model(id="test/no-tools", supported_parameters=[])]
    with pytest.raises(ValueError) as exc:
        fm.filter_and_categorize(models, cfg, required_ids=["test/no-tools"])
    msg = str(exc.value)
    assert "test/no-tools" in msg
    assert "tools" in msg or "filtered out" in msg
