## Plan: Разделение семантики эталона и обязательной выборки в routerai-models

**Progress State**
- **Plan file**: `.github/plans/separated-reference-required-selection.plan.md`
- **Memory index**: Not registered
- **Status**: Implementation complete
- **Base revision**: `af05275be31ef3c6d939a5f02c1db39be99d9ce3` on branch `main`
- **Current phase**: Phase 6 — Dry run & apply (completed)
- **Completed**: Phase 1 (config), Phase 2 (validate_sync_config), Phase 3 (required/excluded_ids), Phase 4 (post-filter validation), Phase 5 (regression tests), Phase 6 (dry run & verification)
- **Last verification**: `python3 -m pytest tests/test_filtering.py tests/test_source.py tests/test_policy.py tests/test_integration_required.py -v` — 50/50 passed. `python3 sync_configs.py` — dry-run shows both DeepSeek IDs included, `ling-3.0-flash` excluded. `effective_reference_id()` returns `(deepseek/deepseek-v4-flash, approved)`. `validate_sync_config()` — OK.
- **Next incomplete phase**: none (all phases complete)
- **Phase dependencies**: All satisfied
- **Blockers / open unknowns**: none

---

TL;DR: Отделяем четыре перегруженные концепции `reference_model`: эталон для сравнения (`id`), одобрение для роли `default_agent` (`approved_ids`), обязательное присутствие в Copilot (`required_ids`) и гейт надёжности (`watchlist`). Целевая конфигурация: `approved_ids` содержит ТОЛЬКО эталон (`deepseek/deepseek-v4-flash`), `v4.1-flash` обязателен в выборке через `required_ids` но НЕ одобрен для роли `default_agent` (остаётся в `watchlist`), старая `0731` добавлена в `watchlist`. Добавлена строгая валидация конфигурации (`validate_sync_config`), fail-fast проверка прохождения required моделей через конвейер фильтрации с точной диагностикой причины отсева, и регрессионные тесты.

---

## Context & Current State

### Существующая архитектура

**Конвейер**: `data/catalog.json` (301 модель) → `filter_and_categorize()` → `select_top_models(total=15)` → `imports/chatLanguageModels.json` + ai-core config sync.

**Ключевые функции и файлы:**
- `filter_config.json` — центральная конфигурация: `reference_model`, `tiers`, `sync.selection`, `validation`, `paths`, `scoring`
- `filter_models.py::filter_and_categorize()` — валидация, фильтрация (modality/tools/pricing/valid_chat_model), присвоение тиров, расчёт стоимости. Возвращает `{stats, tiers, tier_boundaries, reference_model, generated_at}`
- `filter_models.py::is_valid_chat_model()` — проверяет `matches_modality`, `require_tools_param` ("tools" в supported_parameters), `require_positive_pricing`. Вызывается внутри `filter_and_categorize()`.
- `sync_configs.py::select_top_models()` — 6 фаз: parse → total≤0 shortcut → ref first → DefaultAgentGate → quota fill → remainder → relevance sort
- `reliability.py::effective_reference_id()` — Path 1: если configured id в каталоге + approved → return (id, "approved"); Path 2: auto-select cheapest cheap+tools NOT watchlisted → return (id, "auto")
- `reliability.py::is_model_approved()` — `model_id in cfg.reference_model.approved_ids`
- `reliability.py::is_watchlisted()` — `model_id in cfg.reference_model.watchlist`
- `tests/test_filtering.py` — тесты тиров, DefaultAgentGate, effective_reference_id, novelty ceiling
- `tests/test_source.py` — тесты защиты от пустых/битых данных, gate min_valid_models
- `tests/test_policy.py` — тесты provider_policy, pricing_overrides, select_top_models

**DefaultAgentGate (ревью п.1 — центральная механика)**: DefaultAgentGate в `select_top_models()`:
- `gate_blocked` = все модели каталога, где `is_watchlisted()` AND NOT `is_model_approved()`
- `gated(m)` = `m["id"] in gate_blocked` AND (в `default_agent.top` ИЛИ является `ref_id` если нет рекомендаций)
- **ВАЖНО**: `gated()` вызывается ТОЛЬКО внутри quota-fill цикла (Phase E). Модели, добавленные в `result` ДО этого цикла (ref + required_ids), НЕ проходят через gated().
- Если `v4.1-flash` в `required_ids` (Phase C+), он добавляется ДО quota-fill → gated() не вызывается → модель присутствует в выборке, но НЕ как default_agent.

**Текущая конфигурация (`filter_config.json`):**
- `reference_model.id: "deepseek/deepseek-v4-flash"`
- `reference_model.approved_ids: ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash"]`
- `reference_model.watchlist: ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash"]`
- `sync.selection.quotas: {cheap: 10, medium: 2, premium: 3}`, `total: 15`
- Нет полей `required_ids`, `excluded_ids`
- **Проблема**: обе модели одновременно в `approved_ids` (одобрены) и `watchlist` (ждут подтверждения) — противоречие

**Детали фильтрации в `filter_models.py::filter_and_categorize()`:**
1. `matches_modality(m, modality_mode="text_in_text_out")` — модальность
2. `"tools" not in supported_parameters` — проверка tools
3. `pricing.prompt <= 0 or pricing.completion <= 0` — отсутствие/нулевая цена
4. `is_valid_chat_model(m, cfg)` — комплексная проверка mod+tools+pricing
5. `pricing_unit_warning(m)` — non-token единицы (не фильтр, а маркировка)
6. `assign_tier(m, cfg)` — распределение по тирам
- **Статистика**: `stats` содержит счётчики по каждому типу исключения

### Инварианты, которые нельзя нарушить
- DefaultAgentGate: watchlisted + not approved блокирует роль `default_agent`, но не весь отбор (остальные роли и copilot в целом)
- Required модели добавляются ДО quota-fill — gated() не применяется
- Квоты: cheap=10, medium=2, premium=3, total=15 — НЕ МЕНЯТЬ
- Tier boundaries: cheap < 100₽/1M, premium ≥ 100₽/1M + reasoning, medium = остальное — НЕ МЕНЯТЬ
- Selection weights: DEFAULT_SELECTION_WEIGHTS — НЕ МЕНЯТЬ
- Reference model всегда на позиции 0 после relevance sort (гарантируется `relevance_key()`: `is_ref = 0 if m["id"] == ref_id else 1`)
- `filter_and_categorize()` уже содержит `is_valid_chat_model()` — полная проверка. Для точной диагностики нужно переиспользовать те же функции.

---

## Steps

### Phase 1: Обновление filter_config.json

**Зависимости**: Нет.

**Изменения** (`filter_config.json`):

1. **`reference_model.watchlist`** — заменить на `["deepseek/deepseek-v4-flash-0731"]`. Убрать `deepseek/deepseek-v4-flash` из watchlist. **`deepseek/deepseek-v4.1-flash` остаётся в watchlist** (см. п.2 почему).

2. **`reference_model.approved_ids`** — оставить ТОЛЬКО `"deepseek/deepseek-v4-flash"`. Убрать `deepseek/deepseek-v4.1-flash`.
   - **Обоснование**: `v4-flash` одобрен как эталон (Path 1 в `effective_reference_id()`). `v4.1-flash` НЕ одобрен для default_agent. Поскольку `v4.1-flash` остаётся в `watchlist` и НЕ в `approved_ids`, он попадает в `gate_blocked` → DefaultAgentGate может заблокировать его от роли default_agent (хотя required_ids bypass-ит gated, это корректно — он в выборке, но не как default_agent).

3. **`reference_model.id`** — оставить `"deepseek/deepseek-v4-flash"`.

4. **`reference_model.label`** — заменить на `"Эталон: DeepSeek V4 Flash"`.

5. **`sync.selection.required_ids`** — добавить: `["deepseek/deepseek-v4.1-flash"]`.
   - `v4-flash` НЕ включаем — он уже гарантирован как ref_id.

6. **`sync.selection.excluded_ids`** — добавить: `["inclusionai/ling-3.0-flash"]`.

7. **`sync.selection.total`** — оставить `15`. Required ID учитывается в лимите.

**Итоговое состояние reference_model секции:**
```json
"reference_model": {
    "id": "deepseek/deepseek-v4-flash",
    "approved_ids": ["deepseek/deepseek-v4-flash"],
    "watchlist": ["deepseek/deepseek-v4-flash-0731"],
    "fallback": "cheapest_cheap_tools_not_watchlisted",
    "label": "Эталон: DeepSeek V4 Flash"
}
```

**Relevant Files & Symbols**:
- `filter_config.json` — Modify (JSON structure update)

---

### Phase 2: Валидация конфигурации

**Зависимости**: Phase 1 (конфигурация). Может выполняться параллельно с Phase 3-4 (код не пересекается).

**Изменения** (`sync_configs.py`):

Добавить функцию `validate_sync_config(fcfg: dict[str, Any]) -> list[str]`:

**Проверки:**
1. **Типы**: `required_ids` (если есть) — `list[str]`; `excluded_ids` (если есть) — `list[str]`. `isinstance(..., list)` и `all(isinstance(x, str) for x in ...)`.
2. **Дубликаты внутри required_ids**: `set()` — если длина уменьшилась, сообщить дубликаты.
3. **Пересечение required_ids ∩ excluded_ids**: ошибка — модель не может быть обязательной и исключённой.
4. **excluded_ids не содержит reference_model.id**: ошибка.
5. **Сумма квот (quotas) ≤ total**: если total > 0, сумма quotas.values() ≤ total.
6. **total ≥ 0**: отрицательный total — ошибка.
7. **Все id в required_ids — непустые строки**.
8. **Все id в excluded_ids — непустые строки**.

Вызов в `main()` `sync_configs.py` — после загрузки fcfg (L843), до `effective_reference_id()`.
Вызов в `main()` `filter_models.py` — после загрузки cfg (L485), до обработки данных.
При ошибках: `sys.stderr.write(...)` + `return 1`.

**Relevant Files & Symbols**:
- `sync_configs.py` — Добавить `validate_sync_config()`; вызов в `main()` после L843
- `filter_models.py` — Добавить вызов `validate_sync_config()` в `main()` после L485

---

### Phase 3: Реализация required_ids и excluded_ids в select_top_models()

**Зависимости**: Phase 1 (конфигурация). Может выполняться параллельно с Phase 2.

**Изменения** (`sync_configs.py`):

В `select_top_models()` (L239-380) после Phase C (ref first, L277-285: `if ref: result.append(ref); seen.add(ref["id"])`) вставить две новых фазы:

**Phase C+ (Required IDs inclusion)**: после L285, до quota fill (L310)
```python
# Phase C+: Required IDs inclusion (after ref, before quotas)
required_ids = selection.get("required_ids") or []
for req_id in required_ids:
    if req_id in seen:
        continue  # dedup with ref_id
    req_model = by_id.get(req_id)
    if not req_model:
        raise RuntimeError(
            f"Required model {req_id} missing from filtered catalog "
            f"after validation — pipeline inconsistency"
        )
    result.append(req_model)
    seen.add(req_id)
```

**Phase C++ (Exclusion filter)**: сразу после C+, до quota fill
```python
# Phase C++: Exclusion filter (after required, before quotas)
excluded_ids = set(selection.get("excluded_ids") or [])
if excluded_ids:
    for tier in candidates_by_tier:
        candidates_by_tier[tier] = [
            m for m in candidates_by_tier[tier]
            if m["id"] not in excluded_ids and m["id"] not in seen
        ]
```

**Логика квот**: Required модели занимают слоты из total. Quota fill вычисляет `already` по тиру (ref + required), вычитает из квоты. `total` и `quotas` не меняются.

**Relevant Files & Symbols**:
- `sync_configs.py` — Modify `select_top_models()`, insert phases C+ and C++ after L285

---

### Phase 4: Обязательная проверка прохождения required через конвейер фильтрации

**Зависимости**: Phase 1 (required_ids в config). Phase 2 не нужна (разные модули).

**Изменения** (`filter_models.py`):

1. В `filter_and_categorize()` (L228) добавить параметр `required_ids: Optional[list[str]] = None`.

2. Добавить функцию точной диагностики (`_diagnose_exclusion`), переиспользующую те же функции, что и основной фильтр:

```python
def _diagnose_exclusion(
    raw: Optional[dict[str, Any]],
    cfg: dict[str, Any],
    modality_mode: str,
) -> list[str]:
    """Возвращает причины отсева, переиспользуя функции из filter_and_categorize.

    Вызов matches_modality(), проверка "tools" in supported_parameters,
    проверка цены, pricing_unit_warning().
    """
    if raw is None:
        return ["модель отсутствует в исходном каталоге"]
    reasons = []
    if not matches_modality(raw, modality_mode):
        arch = raw.get("architecture") or {}
        reasons.append(f"модальность {arch.get('modality')} несовместима с {modality_mode}")
    require_tools = cfg.get("validation", {}).get("require_tools_param", True)
    if require_tools and "tools" not in (raw.get("supported_parameters") or []):
        reasons.append("отсутствует tools в supported_parameters")
    require_positive = cfg.get("validation", {}).get("require_positive_pricing", True)
    if require_positive:
        pricing = raw.get("pricing") or {}
        p, c = pricing.get("prompt"), pricing.get("completion")
        if not p or not c or p <= 0 or c <= 0:
            reasons.append(f"цена не установлена (prompt={p}, completion={c})")
    if pricing_unit_warning(raw):
        reasons.append("единицы тарификации не token")
    return reasons or ["причина не определена (возможно model validation)"]
```

3. После сбора `valid` и `tiers` (после L368, перед `return`), добавить блок:

```python
    # Проверка required_ids прошли фильтрацию
    if required_ids:
        filtered_ids = {e["id"] for e in valid}
        for rid in required_ids:
            if rid not in filtered_ids:
                raw = next((m for m in models if m.get("id") == rid), None)
                reasons = _diagnose_exclusion(raw, cfg, modality_mode)
                raise ValueError(
                    f"Required model {rid} filtered out: {'; '.join(reasons)}. "
                    f"Cannot proceed."
                )
```

4. В `main()` (`filter_models.py` L505: `result = filter_and_categorize(models, cfg)`) передать required_ids:
```python
    required_ids = cfg.get("sync", {}).get("selection", {}).get("required_ids", [])
    result = filter_and_categorize(models, cfg, required_ids=required_ids or None)
```

**Relevant Files & Symbols**:
- `filter_models.py` — Modify `filter_and_categorize()` signature (L228); add `_diagnose_exclusion()`; add validation block (после L368); update `main()` call site (L505)

---

### Phase 5: Регрессионные тесты

**Зависимости**: Phase 3 (код required/excluded), Phase 4 (post-filter validation), Phase 2 (config validation).

**Новые тесты** (добавить в `tests/test_filtering.py`):

1. **`test_required_ids_included`** — Каталог с required_ids. `select_top_models()` включает их в результат перед квотами. Порядок: ref (position 0), then required, then quota-filled.

2. **`test_required_ids_dedup_with_ref`** — required_ids содержит ref_id. Дубликатов нет — модель одна.

3. **`test_required_ids_missing_from_catalog_raises`** — required_id отсутствует в `by_id` → RuntimeError.

4. **`test_excluded_ids_filtered_from_quota_fill`** — excluded_ids модель не попадает в выборку quota fill. Ref и required не трогаются.

5. **`test_total_limit_respected_with_required`** — total=15, 1 required модель → len(result) ≤ 15.

6. **`test_required_excluded_intersection_error`** — `validate_sync_config()` с пересечением → ошибка.

7. **`test_reference_position_0_after_sort`** — После relevance sort эталон на позиции 0.

8. **`test_validate_sync_config_type_checks`** — required_ids не список → ошибка. required_ids с int → ошибка.

9. **`test_validate_sync_config_duplicates_in_required`** — `required_ids: ["a", "a"]` → ошибка.

10. **`test_validate_sync_config_quotas_exceed_total`** — quotas сумма > total → ошибка.

11. **`test_required_ids_filtered_out_diagnostics`** — `_diagnose_exclusion()` или `filter_and_categorize()` с required моделью, не проходящей фильтрацию (no tools, bad modality, zero pricing) → ValueError с точными причинами отсева.

**Интеграционный тест** (новый файл `tests/test_integration_required.py`):

12. **`test_full_pipeline_required_end_to_end`** — Временный `filter_config.json` в tmp_path. Минимальный каталог (6+ моделей, включая required модель). Прогнать `filter_models.filter_and_categorize()` + `sync_configs.select_top_models()`. Проверить:
    - Оба ID (ref + required) присутствуют в selected
    - `inclusionai/ling-3.0-flash` отсутствует (excluded)
    - Размер ≤ 15
    - Эталон на позиции 0
    - `reliability.effective_reference_id()` возвращает `("v4-flash", "approved")` с обновлённым config
    - required модель НЕ в `default_agent.top` через gated() (если есть рекомендации)

**Relevant Files & Symbols**:
- `tests/test_filtering.py` — Add 11 test functions (use `base_cfg()`, `mk_model()`, `sc.select_top_models()`)
- `tests/test_integration_required.py` — New file (end-to-end with tmp_path config, import filter_models + sync_configs + reliability)

---

### Phase 6: Сухая прогонка и применение

**Зависимости**: Все предыдущие фазы.

**Действия**:
1. `python -m pytest tests/test_filtering.py tests/test_source.py tests/test_policy.py tests/test_integration_required.py -v`
2. `python sync_configs.py --help` — исследовать CLI. `sync_configs.py` по умолчанию dry-run (см. `--dry-run` в коде: `parser.add_argument("--dry-run", ...)`; флаг `--apply` для реальной записи).
3. `python sync_configs.py` — проверить diff.
4. `git diff imports/chatLanguageModels.json` — оба DeepSeek ID присутствуют, `ling-3.0-flash` отсутствует.
5. `python3 -c "from reliability import effective_reference_id; import json; cfg=json.load(open('filter_config.json')); cat=json.load(open('data/catalog.json')); eid, st=effective_reference_id(cfg, cat); print(eid, st)"` → `deepseek/deepseek-v4-flash approved`.
6. `python3 -c "from sync_configs import validate_sync_config; import json; errs=validate_sync_config(json.load(open('filter_config.json'))); assert not errs, chr(10).join(errs); print('Validation OK')"`
7. Применить: `python sync_configs.py --apply` (только после утверждения).

**Relevant Files & Symbols**:
- Команды в терминале
- `imports/chatLanguageModels.json` — verify output

---

## Verification

1. **Тесты**: `pytest tests/test_filtering.py tests/test_source.py tests/test_policy.py tests/test_integration_required.py -v` — все 20+ тестов зелёные
2. **Dry-run**: `python sync_configs.py` — diff показывает оба DeepSeek ID, `ling-3.0-flash` отсутствует
3. **effective_reference_id**: возвращает `(deepseek/deepseek-v4-flash, 'approved')`
4. **validate_sync_config**: ни одной ошибки на корректном конфиге; обнаруживает пересечение/дубликаты/типы
5. **filter_and_categorize с required_ids**: ValueError с точным диагнозом причины отсева
6. **DefaultAgentGate**: `v4.1-flash` в `gate_blocked` (watchlisted + not approved), НО присутствует в выборке (required_ids bypass-ит gated)

---

## Risks & Rollback

- **Риск**: Удаление `v4.1-flash` из `approved_ids` меняет поведение `effective_reference_id()` — Path 1 не сработает для `v4.1-flash`. **Неактуально**: эталон остаётся `v4-flash`, для `v4.1-flash` это изменение желательно.
- **Риск**: `v4.1-flash` в `watchlist` может быть случайно заблокирован DefaultAgentGate. **Компенсация**: required_ids добавляется до gated() — модель в выборке, но не default_agent.
- **Риск**: Required модель не прошла фильтрацию каталога → ValueError → обрыв конвейера. **Компенсация**: это желательное fail-fast поведение.
- **Rollback**: `git checkout filter_config.json sync_configs.py filter_models.py`

---

## Decisions & Scope Boundaries

- **Included**: Конфигурация, required_ids/excluded_ids, validate_sync_config, _diagnose_exclusion, регрессионные тесты
- **Excluded**: Изменение DefaultAgentGate, tier boundaries, selection weights, quotas. Изменение `effective_reference_id()` или других функций `reliability.py`. Обновление `data/catalog.json`.
- **Key Decisions**:
  1. `approved_ids` = только `["deepseek/deepseek-v4-flash"]`. `v4.1-flash` — required, не approved. `0731` — watchlist.
  2. `v4.1-flash` остаётся в `watchlist` (DefaultAgentGate может заблокировать default_agent). required_ids bypass-ит gated().
  3. Пересечение `required_ids` ∩ `excluded_ids` — ошибка конфигурации.
  4. Post-filter проверка — ПОСЛЕ фильтрации с переиспользованием `matches_modality()`, `tools` in params, pricing checks, `pricing_unit_warning()`.
  5. Required модели занимают слоты из total=15.
  6. Tier boundaries, quotas, weights, scoring — без изменений.

---

## Decision Record

- **approved_ids cleanup**: Problem: обе модели в approved_ids запутывали семантику; ревью п.1 требует блокировки default_agent для v4.1-flash → Evidence: current config has both in approved_ids AND watchlist → Options: (A) keep both, (B) only ref in approved_ids, (C) remove both → Selected: B — v4-flash approved, v4.1-flash NOT approved, old 0731 on watchlist
- **Watchlist для v4.1-flash**: Problem: ревью требует v4.1-flash заблокирован для default_agent → Evidence: DefaultAgentGate = watchlisted AND NOT approved → Selected: v4.1-flash stays in watchlist, removed from approved_ids → Why: gate_blocked includes v4.1-flash; required_ids bypass-ит gated() (added before quota fill) → модель в выборке (через required), не default_agent
- **Точная диагностика (ревью п.4)**: Problem: старая версия давала приблизительные причины отсева → Evidence: все проверки уже есть в filter_and_categorize (matches_modality, "tools" in params, pricing, pricing_unit_warning) → Selected: _diagnose_exclusion переиспользует те же функции → Why: 100% совпадение с поведением фильтра
- **validate_sync_config усиление (ревью п.3)**: Problem: старая версия не проверяла типы и дубликаты → Selected: type checks (list[str]), duplicate detection, quotas ≤ total, empty strings → Why: catch misconfiguration early
- **Тест default_agent (ревью п.5)**: Problem: тесты не проверяли effective_reference_id после изменений → Selected: интеграционный тест проверяет effective_reference_id() output + gated() для required модели с обновлённым config → Why: полный цикл от конфига до роли
- **Сохранение границ/весов/квот (ревью п.6)**: Problem: границы тиров не должны меняться → Selected: явно зафиксировать в Invariants → Why: план не меняет tiers_cfg, quotas, weights, scoring

---

## Implementation Contract

### Phase 1 — Configuration update
- **Base revision**: `af05275be31ef3c6d939a5f02c1db39be99d9ce3` on main
- **Allowed files**: `filter_config.json`
- **Required changes**:
  - `reference_model.watchlist` → `["deepseek/deepseek-v4-flash-0731"]`
  - `reference_model.approved_ids` → `["deepseek/deepseek-v4-flash"]`
  - `reference_model.label` → `"Эталон: DeepSeek V4 Flash"`
  - Add `sync.selection.required_ids: ["deepseek/deepseek-v4.1-flash"]`
  - Add `sync.selection.excluded_ids: ["inclusionai/ling-3.0-flash"]`
  - Preserve all existing `quotas`, `weights`, `tiers`, `scoring`
- **Preserve**: All other config sections unchanged
- **Out of scope**: Any Python code
- **Verification**: `python3 -c "import json; c=json.load(open('filter_config.json')); a=c['reference_model']['approved_ids']; assert a==['deepseek/deepseek-v4-flash'], f'approved_ids={a}'; w=c['reference_model']['watchlist']; assert w==['deepseek/deepseek-v4-flash-0731'], f'watchlist={w}'; r=c['sync']['selection']['required_ids']; assert r==['deepseek/deepseek-v4.1-flash'], f'required_ids={r}'; x=c['sync']['selection']['excluded_ids']; assert x==['inclusionai/ling-3.0-flash'], f'excluded_ids={x}'; print('OK')"`
- **Stop conditions**: Config file unreadable or locked

### Phase 2 — Config validation
- **Base revision**: Same
- **Allowed files**: `sync_configs.py`, `filter_models.py`
- **Required changes**: Add `validate_sync_config(fcfg) -> list[str]`; call in both `main()` entry points
- **Preserve**: All existing pipeline logic
- **Out of scope**: Changes to select_top_models, filter_and_categorize
- **Verification**: `python3 -c "from sync_configs import validate_sync_config; e=validate_sync_config({'sync':{'selection':{'required_ids':['a'],'excluded_ids':['a']}}}); assert len(e)==1, e"`
- **Stop conditions**: Cannot determine correct main() entry point

### Phase 3 — required_ids + excluded_ids in select_top_models
- **Base revision**: Same
- **Allowed files**: `sync_configs.py`
- **Required changes**: Insert Phase C+ (required) and C++ (excluded) between ref-first (L285) and quota-fill loop (L310)
- **Preserve**: All existing phases, DefaultAgentGate (gated()), quota fill, relevance sort
- **Out of scope**: filter_models.py, config validation, tests
- **Verification**: Unit test simulation confirming required included, excluded filtered, total respected
- **Stop conditions**: Cannot identify correct insertion point

### Phase 4 — Post-filter required validation
- **Base revision**: Same
- **Allowed files**: `filter_models.py`
- **Required changes**: Add `required_ids` param to `filter_and_categorize()`; add `_diagnose_exclusion()`; add post-filter validation block; update `main()` call site
- **Preserve**: Existing validation, tier assignment, cost calculation. Signature backward-compatible (default None).
- **Out of scope**: sync_configs.py
- **Verification**: `python3 -c "from filter_models import filter_and_categorize; filter_and_categorize([], {'validation':{}, 'tiers':{}, 'reference_model':{}}, required_ids=['missing/model'])"` → ValueError with reason
- **Stop conditions**: Cannot locate filter_and_categorize call site in main()

### Phase 5 — Regression tests
- **Base revision**: Same
- **Allowed files**: `tests/test_filtering.py`, `tests/test_integration_required.py` (NEW)
- **Required changes**: Add 11 test functions in test_filtering.py + 1 integration test in test_integration_required.py
- **Preserve**: All existing tests (must pass)
- **Out of scope**: Production code changes
- **Verification**: `pytest tests/test_filtering.py tests/test_integration_required.py -v` — all pass
- **Stop conditions**: Test framework unavailable; existing tests broken by production code changes

### Phase 6 — Dry run and apply
- **Base revision**: Same
- **Allowed files**: None (verification only)
- **Required changes**: Run commands, inspect output
- **Preserve**: Nothing (read-only verification)
- **Out of scope**: Code changes
- **Verification**: `effective_reference_id()` → approved; `imports/chatLanguageModels.json` → both DeepSeek IDs, no ling, ≤15, ref pos 0
- **Stop conditions**: Dry run shows unexpected model composition

---

## Further Considerations

1. **Назначение default_agent**: required_ids гарантирует присутствие модели в наборе Copilot, но не разрешает роль default_agent. DefaultAgentGate в quota-fill loop не затрагивает required модели (добавлены до цикла). Однако реальная проверка роли требует доступа к `recommendations.json` — если там `v4.1-flash` в `default_agent.top`, gated() сработал бы, но required bypass-ит его. В интеграционном тесте нужно создать рекомендации без v4.1-flash в default_agent.top, чтобы проверить корректность.
2. **0731 в watchlist**: Старая версия `deepseek/deepseek-v4-flash-0731` должна быть в watchlist. Если её НЕТ в каталоге — это безопасно: watchlist влияет только на gate_blocked, несуществующая модель не попадёт в gate_blocked. Валидация существования watchlist-моделей не требуется (watchlist — гейт для будущего, не для текущего каталога).
3. **sync_configs.py использует `ref_cfg`** (полный fcfg, не только selection) при построении `gate_blocked`. Это важно: `ref_cfg` передаётся в `is_watchlisted` и `is_model_approved`, которые читают `reference_model.watchlist` и `reference_model.approved_ids` из fcfg — то есть изменения конфига Phase 1 корректно повлияют на DefaultAgentGate.
