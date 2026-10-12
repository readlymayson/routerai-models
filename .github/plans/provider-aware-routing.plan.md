## Plan: Provider-aware routing & route pricing

**Progress State** (single source of truth for resumption — update in place, never append history)
- **Plan file**: `.github/plans/provider-aware-routing.plan.md` (репо `routerai-models`) — канонический файл, который обновляет Enagent; MEMORY INDEX НЕ ЗАМЕНА
- **Memory index**: Registered (`/memories/repo/plan-index.md`)
- **Status**: Not started — updated 2026-10-12
- **Base revision**: routerai-models `e823358724b9747ea472adc1b84af899a46d21be` on branch `main`; ai-core `2f74a6bc202ec3aef298320062d3527feb6a70c6` on branch `main`. Если HEAD Enagent отличается — сначала переверить план по текущему репо (особенно `sync_configs.py`, `core/llm_engine.py`, `core/config_manager.py`)
- **Current phase**: none (planning finished)
- **Completed**: none
- **Last verification**: не выполнялось
- **Next incomplete phase**: Phase 1 — Endpoint-каталог и route-цены (routerai-models, только данные) → первое действие: добавить `endpoints.py` с чистыми парсерами и `fetch_endpoints()` + запись `data/endpoints.json`, покрыть `tests/test_endpoints.py`
- **Phase dependencies**: Phase 2 ← Phase 1 (артефакт `data/endpoints.json`); Phase 3 ← Phase 2 (артефакты `provider_policy` и `pricing_overrides` в config.json); Phase 4 ← Phase 3
- **Blockers / open unknowns**: `ROUTERAI_API_KEY` есть в `routerai-models/.env`, но `fetch_models.py` читает его из **env-окружения** (`os.environ`), а systemd-юнит переменную не задаёт — Phase 1 обязана пробросить `env_file`/`Environment=` либо задокументировать источник ключа, иначе эндпоинты выгружать нечем (гейт для Phase 1). ai-core параллельно правят другие планы (`agency-concurrency-races-fix` Phase 4 идёт; `web-chat-sessions-roles-models` не начат) — Phase 3 трогает те же файлы, координировать порядок.

RouterAI различает **модель** (`deepseek/deepseek-v4.1-flash`) и **провайдера** — конкретный endpoint этой модели. Без параметра `provider` шлюз сам выбирает маршрут и тарифицирует **фактически использованный** endpoint, а не минимальную цену модели в каталоге. Для одной модели разброс цен кратен: проверено живьём для `deepseek/deepseek-v4.1-flash` — 30 endpoint'ов, `tag=deepseek` = 18.2 ₽/1M (prompt) против агрегированной цены каталога 4.22 ₽/1M. План добавляет второй уровень данных (endpoint'ы), второй уровень цены (цена маршрута) и политику выбора провайдера, доведённую до тела API-запроса и до учёта затрат.

**Context & Current State**

_Проверенные факты (не предположения):_

- **API endpoint'ов существует и подтверждён живьём.** `GET https://routerai.ru/api/v1/models/{model_id}/endpoints` → `{"data": {"id", "name", "architecture", "created", "description", "endpoints": [...]}}`. Каждый endpoint: `tag`, `provider_name`, `name`, `status`, `quantization`, `country`/`countries`, `context_length`, `max_completion_tokens`, `supported_parameters` (содержит `tools`/`tool_choice`/`reasoning`), `supported_apis` (содержит `chat`), `pricing.{prompt,completion,input_cache_read}` (₽/токен), `pricing_units`, `variable_pricings[]` (time-of-day, `utc_start/utc_end/utc_days`). У `deepseek/deepseek-v4.1-flash` вернулось 30 endpoint'ов, `endpoints[0].tag == "deepseek"`.
- **Каталог моделей (`/api/v1/models`) endpoint'ов НЕ содержит.** Ключи записи каталога: `architecture, context_length, created, default_parameters, description, id, name, per_request_limits, pricing, pricing_units, supported_parameters`. `data/routerai_models_raw.json` = 559 моделей. Значит нужен отдельный сетевой вызов на модель.
- **Генератор конфига провайдера не пишет.** `sync_configs.to_ai_core_model(m, base_url)` (sync_configs.py:441) возвращает ровно `{id, name, url, toolCalling, vision, maxInputTokens, maxOutputTokens}`; `to_clm_model` (458) делегирует ему. Поле `_meta` с ценами есть только в промежуточном `data/config_models.json` (там же `pricing_rub_per_1m` и `cost_per_turn_estimate_rub`), в config.json оно не переносится.
- **Цены каталога агрегированные.** `filter_models.rub_per_1m(price) = price * 1e6` (₽/1M); `pricing.prompt` в каталоге = агрегат по маршрутам (4.22 vs 18.2 на одном endpoint). Тир `cheap`/`medium`/`premium` и `cost_per_turn_estimate_rub` считаются от агрегата (filter_config `call_params`: prompt 10000, completion 1000).
- **ai-core точка вставки тела запроса уже существует, но не используется.** `OpenAICompatibleProvider.generate_response` (core/llm_engine.py:522) делает `payload.update(extra_body)` (llm_engine.py:626–628) ДО отправки; `extra_body` проброшен из `LLMEngine.generate_response(..., extra_body=...)` (1372, 1476). Grep по репо: **ни один вызывающий `extra_body` не передаёт**. То есть политику провайдера достаточно начать вычислять в `LLMEngine` и передавать вниз — менять провайдер-класс не нужно.
- **Провайдер определяется префиксом модели.** `LLMEngine.generate_response` (1404): `routerai:deepseek/...` → `provider="routerai"`, модель = хвост. `_setup_providers` (1283): `routerai` всегда `OpenAICompatibleProvider(api_key, config.routerai_base_url)`. RouterAI-модели идут **только** через `provider="routerai"`; гейт «это routerai» = `provider == "routerai"` (или `base_url` содержит `routerai.ru`). DeepSeek-direct (`api.deepseek.com`) и Yandex политику получать не должны.
- **Учёт затрат цен каталога НЕ использует.** `core/cost_estimator.py`: `DEFAULT_PRICING` (хардкод, ₽-эквиваленты «CU»), `CostEstimator(model_overrides: Dict[str, Dict[str, float]])`, `get_pricing` ищет по `model_id` → `model_overrides` → короткий хвост после `/` → `default`. Ключи overrides — `prompt_token`/`completion_token` (₽ за 1 токен). Синглтон `get_cost_estimator()` **инициализируется без overrides** — `config.json.llm.model_overrides` = `{}` и к синглтону не подключён. `LocalCostTracker` (core/local_cost_tracker.py:45) берёт `get_cost_estimator()`.
- **Конфиг-контракт ai-core.** `llm.providers.routerai.models[]` читается в `LLMProviderConfig.models` (config_manager.py:378–384); `merge_catalog_models_into_catalog` (315; вызывается из `config_manager.py:2183`) дополняет `MODEL_CATALOG` из этих записей (tier из `_meta.tier`). `LLMConfig` (388) уже имеет `model_overrides: Dict[str, str]` (роль→модель). `LLMConfigWrapper` (main.py:3262) — мост конфига в `LLMEngine`.

_Контракты и инварианты, которые нельзя нарушать:_

- `llm.providers.routerai.models[]` — форма записи: `{id, name, url, toolCalling, vision, maxInputTokens, maxOutputTokens}` (+ опц. `_meta`). `maxInputTokens/maxOutputTokens` считает `ai_core_paths.apply_token_limits` по тиру (filter_config `token_limits`: cheap 100000/16000, medium 80000/8000, premium 64000/8000) — сохранять.
- Единицы цены в config.json для CostEstimator — **₽ за 1 токен** (`prompt_token`/`completion_token`); в каталоге routerai-models — **₽ за 1M**. Не смешивать; конвертировать при записи.
- `_meta.tier` (cheap/medium/premium) — вход тиринга в ai-core. Тиры Phase 1–2 не менять (иначе дрейф счётчиков и регрессии в `recommend`/`sync`).
- `MODEL_CATALOG` статику не перезаписывать (приоритет захардкоженных записей).
- Гарантии защиты записи routerai-models сохранить: без `--apply` целевые файлы не меняются; при пустом/устаревшем каталоге запись блокируется (`ai_core_paths.check_catalog_ok`).
- `MY` — тир/цены `data/config_models.json` остаются обратно совместимыми (потребители: `compare_with_config`, `recommend`).
- ai-core: `provider == "routerai"` — единственный путь, где допустим `extra_body.provider`; иначе не добавлять поле.

**Steps**

1. **Phase 1 — Endpoint-каталог и route-цены (routerai-models, только данные).** _can run in parallel with nothing (первая фаза)._
   - Новый модуль `endpoints.py`: чистые функции — `parse_endpoints(payload) -> list[Endpoint]` (нормализует один endpoint: `tag`, `provider_name`, `status`, `quantization`, `context_length`, `max_completion_tokens`, `supported_apis`, `supported_parameters`, `rub_per_1m.{prompt,completion,input_cache_read}` из `pricing`×1e6, флаг `supports_tools` = `"tools" in supported_parameters`); `endpoint_rub_per_1m(ep)`; `cheapest_endpoint(eps)`; `endpoints_with_tools(eps)`; `route_vs_aggregate(aggregate_rub_per_1m, eps)` (min/median/max и дельта).
   - Сетевой слой `fetch_endpoints(model_ids, cfg, api_key) -> dict[str, list[Endpoint]]`: `GET {base_url}/models/{id}/endpoints`, атомарная запись `data/endpoints.json` (tmp+replace, как `fetch_models.save_raw_data`). Ретраи/таймаут как в `fetch_models`.
   - **Ограничить объём выгрузки**: эндпоинты тянуть не для всех 559 моделей, а для множества из `filter_config.endpoints.scope` — по умолчанию «selected + reference + asr» (после `recommend`/`sync`-отбора уже есть список топ-15 + эталон + ASR). Обоснование: 559 последовательных вызовов недопустимы; политика и route-цены нужны только для моделей, реально попадающих в config.json.
   - Пробросить `ROUTERAI_API_KEY`: добавить `env_file=.env` в `systemd/routerai-models-fetch.service` **или** читать ключ из `.env` в `fetch_endpoints`; задокументировать в README (шаг записи ключа). Без этого фаза не может быть зелёной — это гейт фазы.
   - `filter_config.json`: новый блок `endpoints` (`scope`, `timeout_seconds`, `stale_after_hours`, `min_endpoints_per_model`).
   - `run_all.py`: новый шаг `fetch_endpoints` (после `recommend`, до `sync_configs`); на `--no-fetch` — использовать существующий `data/endpoints.json`.
   - `fetch_models.save_raw_data`/`meta` дополнить отметкой актуальности endpoint-каталога.
   - Tests: `tests/test_endpoints.py` (парсинг формы ответа, `supports_tools` по `supported_parameters`, `rub_per_1m` из `pricing`, `cheapest_endpoint`, `route_vs_aggregate`, атомарность записи, отсутствие модели → пустой список, а не падение).
   - **Verification**: `pytest tests/test_endpoints.py -q` → все зелёные; `python3 -c` проверка `data/endpoints.json` содержит `deepseek/deepseek-v4.1-flash` с ≥1 endpoint и `tag=deepseek`; `run_all.py --no-fetch` не падает на новом шаге.

2. **Phase 2 — Route-цены и provider-политика в каталоге и синхронизаторе (routerai-models).** _depends on Phase 1._
   - `build_report.py`/`filter_models.py`: к `_meta` модели добавить `routes: {n, tools, min_cost_per_turn_estimate_rub, median_cost_per_turn_estimate_rub, max_cost_per_turn_estimate_rub, cheapest_tag, aggregate_minus_cheapest_percent}` — считать из `data/endpoints.json`, фолбэк на пустой блок, если endpoint'ов нет. `cost_per_turn_estimate_rub` (агрегатный) НЕ переопределять — добавить рядом `route_cost_per_turn_estimate_rub`.
   - `sync_configs.py` — новые записи:
     - `llm.providers.routerai.provider_policy` (из `filter_config.provider_policy`): `{order: [tag,...], allow_fallbacks: bool, policy: "order"}` (механизм A, подтверждён пользователем). Валидация: `order[0]` обязан присутствовать в endpoint'ах **всех** отобранных моделей, иначе фолбэк на пустой order (политику не писать).
     - `llm.providers.routerai.pricing_overrides`: `{ "<full model id>": {prompt_token, completion_token} }` — ₽/токен. Источник: если для модели пинован `preferred_tag` (из `filter_config.provider_policy.per_model` или гейта роли) — цена этого endpoint'а; иначе — **минимальная** route-цена (как оптимистичная оценка), с записью `_meta.pricing_basis: "route_min" | "pinned"`. Документировать, что `route_min` — не гарантированная фактическая стоимость.
     - «Липкость»: существующие `provider_policy`/`pricing_overrides` в config.json при `--apply` сохранять, если `filter_config` их не задаёт (тот же паттерн, что Phase 4 плана `routerai-models-catalog-reliability`).
   - `compare_with_config.py`: отчёт по расхождению `provider_policy`/`pricing_overrides` (новые/исчезнувшие/изменившиеся записи) — отдельным блоком, не ломая текущий вывод.
   - Tests: `tests/test_policy.py` (policy формируется только при валидном order; route-цена в ₽/токен = route_rub/1e6; sticky-поведение; `_meta.routes` в `catalog.json`); расширить `tests/test_source.py` на форму `endpoints.json`.
   - **Verification**: `pytest -q` → зелёные (существующие + новые); `python3 sync_configs.py` (dry-run) показывает непустой дифф `provider_policy`/`pricing_overrides`; `sha256 config/config.json` ai-core ДО == ПОСЛЕ без `--apply` (инвариант защиты записи).

3. **Phase 3 — Потребление в ai-core: политика в теле запроса + route-цена в учёте затрат.** _depends on Phase 2 (артефакты в config.json)._
   - `core/config_manager.py`: `LLMProviderConfig` дополнить `provider_policy: Dict[str, Any]` и `pricing_overrides: Dict[str, Dict[str, float]]`; заполнять их в загрузке конфига (рядом с `models=pdata.get('models', [])`). `LLMConfigWrapper` (main.py:3262) — экспонировать `routerai_provider_policy` и `routerai_pricing_overrides`.
   - `core/llm_engine.py`: в `LLMEngine.generate_response` перед вызовом провайдера — если итоговый `provider == "routerai"` и политика непустая, собрать `policy_body = {"provider": {"order": order, "allow_fallbacks": bool}}` и объединить с `extra_body` вызывающего так, что **явный `extra_body` вызывающего выигрывает** при конфликте. Политика не добавляется ни для `deepseek`, ни для `yandex`. Провайдер-класс не менять — он уже делает `payload.update`.
   - `core/cost_estimator.py`: дабавить `CostEstimator.load_overrides(dict)` (слияние в `model_overrides`, совместимо с `get_pricing`); сохранить fallback-каскад `model_id → overrides → short → default`. `get_cost_estimator()` не ломать (синглтон без overrides для тестов).
   - Точка инициализации: при старте (main.py, рядом с созданием `ConfigManager`/`LLMConfigWrapper`) вызвать `get_cost_estimator().load_overrides(config_manager.llm_config.providers['routerai'].pricing_overrides)` — с `try/except` (graceful degradation, как у `usage_callback`). Не перезаписывать существующие CU-цены статики; overrides только для id, пришедших из каталога.
   - Tests: `tests/test_provider_policy.py` — (a) payload routerai-запроса содержит `provider.order`/`allow_fallbacks`; (b) для `deepseek:`/`yandex` поля нет; (c) `extra_body` вызывающего переопределяет политику; (d) пустая политика → поведение прежнее. `tests/test_cost_overrides.py` — (a) override применяется к CCU-расчёту; (b) отсутствующий id → `default` + warning; (c) `load_overrides` не портит статику.
   - **Verification**: `./venv/bin/python -m pytest tests/test_provider_policy.py tests/test_cost_overrides.py -q` → зелёные; регрессионный прогон `./venv/bin/python -m pytest tests/test_openai_provider_retry.py -q` → зелёный (тело запроса не сломано); `ruff` чисто по изменённым файлам.

4. **Phase 4 — Сквозная проверка и документация.** _depends on Phase 3._
   - Интеграционный тест «конфиг → тело запроса → цена»: с фиктивным `ConfigManager` с `provider_policy` и `pricing_overrides`, проверить, что `LLMEngine.generate_response("routerai:...")` кладёт `provider` в payload (мок httpx, как в `test_openai_provider_retry.py`) и что `CostEstimator` считает по route-цене, а не по `DEFAULT_PRICING`.
   - Проверка «каталог → config.json»: прогон `run_all.py` в dry-run, затем `--apply` в **копию** config.json (не в живой) — подтвердить идемпотентность (второй прогон не меняет файл).
   - README обоих репо: раздел про provider-политику, route-цены, ограничение `route_min` (оценка ≠ факт), как получить `ROUTERAI_API_KEY`, как закрепить endpoint (`order` c одним tag).
   - **Verification**: оба интеграционных теста зелёные; `run_all.py` exit 0; `compare_with_config.py` без ошибок; прогон `--apply` в копию дважды → sha256 второго прогона == первого.

**Relevant Files & Symbols**

- `routerai-models/endpoints.py` — Create. Парсеры endpoint'ов (`parse_endpoints`, `cheapest_endpoint`, `endpoints_with_tools`, `route_vs_aggregate`) + `fetch_endpoints()` с атомарной записью `data/endpoints.json`.
- `routerai-models/filter_config.json` — Modify. Новые блоки `endpoints` (`scope`, `stale_after_hours`, `min_endpoints_per_model`) и `provider_policy` (`order`, `allow_fallbacks`, `per_model`).
- `routerai-models/fetch_models.py` — Reuse `save_raw_data` (атомарная запись) как образец; Modify `main` — проброс `ROUTERAI_API_KEY` из `.env`.
- `routerai-models/run_all.py` — Modify `STEPS`/`main`: новый шаг `fetch_endpoints` между `recommend` и `sync_configs`.
- `routerai-models/filter_models.py` / `build_report.py` — Modify: `_meta.routes` и `route_cost_per_turn_estimate_rub` в `data/catalog.json`/`config_models.json`.
- `routerai-models/sync_configs.py` — Modify `to_ai_core_model` (форма записи модели не меняется), новые писатели `provider_policy`/`pricing_overrides`, sticky-сохранение; точка входа `update_ai_core_config` (571).
- `routerai-models/compare_with_config.py` — Modify: блок диффа политики/цен.
- `routerai-models/tests/test_endpoints.py`, `tests/test_policy.py` — Create; `tests/test_source.py`, `tests/test_filtering.py` — Modify.
- `routerai-models/systemd/routerai-models-fetch.service` (или `.timer`-юнит) — Modify: `env_file`/`Environment=ROUTERAI_API_KEY`.
- `ai-core/core/config_manager.py` — Modify `LLMProviderConfig` (378) + загрузка конфига (~1460); `merge_catalog_models_into_catalog` (315) — Reuse без изменений формы.
- `ai-core/core/llm_engine.py` — Modify `LLMEngine.generate_response` (1372): сборка `extra_body.provider` по политике, гейт `provider == "routerai"`. `OpenAICompatibleProvider` (522) — Reuse без изменений.
- `ai-core/core/cost_estimator.py` — Modify `CostEstimator`: `load_overrides()`; `get_cost_estimator` (292) — Reuse.
- `ai-core/main.py` — Modify `LLMConfigWrapper` (3262) + инициализация overrides при старте.
- `ai-core/tests/test_provider_policy.py`, `tests/test_cost_overrides.py` — Create.
- `ai-core/config/config.example.json` — Modify: примеры `provider_policy`/`pricing_overrides` (без секретов).

**Verification**

1. Phase 1: `cd routerai-models && pytest tests/test_endpoints.py -q` → все зелёные; `data/endpoints.json` содержит `deepseek/deepseek-v4.1-flash` с `tag="deepseek"` в списке.
2. Phase 2: `cd routerai-models && pytest -q` → все зелёные (старые + новые); `python3 sync_configs.py` (dry-run) печатает непустой дифф `provider_policy`/`pricing_overrides`; `sha256sum /home/dev/projects/ai-core/config/config.json` не меняется после dry-run.
3. Phase 3: `cd ai-core && ./venv/bin/python -m pytest tests/test_provider_policy.py tests/test_cost_overrides.py tests/test_openai_provider_retry.py -q` → все зелёные; `./venv/bin/ruff check core/llm_engine.py core/cost_estimator.py core/config_manager.py main.py` → чисто.
4. Phase 4: интеграционные тесты зелёные; `cd routerai-models && python3 run_all.py --no-fetch` → exit 0; двойной `--apply` в копию config.json → sha256 не меняется между прогонами.

**Risks & Rollback**

- **Политика ломает не-routerai провайдеры / 400 от API.** Смягчение: гейт по `provider == "routerai"` + тест (b) в Phase 3; `allow_fallbacks: true` по умолчанию. Откат: убрать `provider_policy` из config.json — код молча вернётся к прежнему телу запроса (пустая политика = no-op).
- **Route-цены «route_min» выдаются за факт.** Это оптимистичная оценка: шлюз тарифицирует фактический endpoint. Смягчение: `_meta.pricing_basis`, README-предупреждение, `provider_policy.order` для контроля маршрута. Откат: удалить `pricing_overrides` — CostEstimator вернётся к `DEFAULT_PRICING`.
- **559 сетевых вызовов / rate-limit.** Смягчение: `endpoints.scope = selected + reference + asr`, таймауты и `min_endpoints_per_model`; сбой выгрузки эндпоинтов не блокирует конвейер (фолбэк на пустые `routes`).
- **Конкурентные правки ai-core.** Другие активные планы трогают `core/llm_engine.py`/`config_manager.py`. Смягчение: Phase 3 делать после ре-верификации HEAD; конфликты решать в пользу существующих планов.
- **Дрейф кэша CLM/VS Code.** Новые поля в `build_clm_provider` не добавлять (VS Code clm не знает `provider`), иначе ломается схема `chatLanguageModels.json`. Смягчение: политику писать **только** в ai-core config.json.
- Rollback фаз: routerai-models — `git revert` коммита фазы (данные в `data/` в `.gitignore`, восстанавливаются прогоном); ai-core — `git revert` + удаление `provider_policy`/`pricing_overrides` из config.json.

**Decisions & Scope Boundaries**

- **Included**: endpoint-каталог и route-цены в `routerai-models`; запись `provider_policy` + `pricing_overrides` в ai-core config.json генератором; чтение политики и проброс её в тело RouterAI-запроса в ai-core; route-цены в `CostEstimator`; тесты обоих репо; документация.
- **Excluded**: изменение тиринга (cheap/medium/premium) и `cost_per_turn_estimate_rub`; правки `chatLanguageModels.json`-схемы; UI/дашборды; авто-выбор провайдера «обучением» (эвристика по совместимости вне объёма); `@tag`-пин в строке модели (механизм B отклонён); изменение самих провайдер-классов `DeepSeekProvider`/`YandexGPTProvider`.
- **Key Decisions**: механизм политики — `extra_body.provider = {order, allow_fallbacks}` (A); хранение — глобальный `llm.providers.routerai.provider_policy` + `pricing_overrides` (A); охват — сквозной, оба репо (A); эндпоинты грузятся только для отобранных моделей.

**Decision Record**

- **Механизм передачи политики**: Problem — политику нужно доставить в тело API, но провайдер-класс общий. Evidence — `payload.update(extra_body)` уже есть (llm_engine.py:626), вызовов `extra_body` нет. Options — A `extra_body.provider{order,allow_fallbacks}`; B суффикс `@tag`; C оба. Selected — A. Why — структурированный список предпочтений + резерв, авто-резервирование сохраняется, точка вставки существует, сравнение моделей не ломается. Rejected — B: абсолютная привязка без fallback, 404 при несовместимости, не выразить список.
- **Где жить политике**: Problem — 15 отобранных моделей + роли. Evidence — `LLMConfig.model_overrides` (роль→модель) уже есть; `LLMProviderConfig` не имеет полей политики. Options — A глобальный блок на провайдера + per-role overrides; B поле в каждой записи модели; C отдельный файл. Selected — A. Why — не дублировать 15 блоков, ролям нужны разные требования к совместимости, каталог endpoint'ов валидирует теги. Rejected — B: дублирование, не масштабируется на роли.
- **Единицы route-цены**: Problem — CostEstimator ждёт ₽/токен, каталог даёт ₽/1M. Evidence — `PricingTier.prompt_token` vs `filter_models.rub_per_1m = ×1e6`. Options — писать ₽/1M и конвертировать в коде; писать ₽/токен. Selected — ₽/токен в `pricing_overrides` (как ждёт `get_pricing`). Why — нулевая конвертация на рантайме, нет риска двойного ×1e6. Rejected — ₽/1M: конвертацию пришлось бы помнить в каждом потребителе.
- **Источник цены маршрута при нескольких endpoint'ах**: Problem — какой endpoint считать. Evidence — 30 endpoint'ов у одной модели, разброс 4.22→390 ₽/1M. Options — минимальная route-цена; цена preferred-тега; средняя. Selected — pinned-тег, если задан, иначе минимум с флагом `pricing_basis="route_min"`. Why — предсказуемо и объяснимо; минимум = нижняя граница бюджета. Rejected — средняя: размывает и не отражает ни один реальный маршрут.

**Implementation Contract** (per phase — Enagent's binding sandbox)

- **Phase 1**
  - Base revision: routerai-models `e823358`
  - Allowed files: `endpoints.py` (new), `filter_config.json`, `run_all.py`, `fetch_models.py`, `systemd/*` (только env-строка), `tests/test_endpoints.py` (new), `README.md`, `data/endpoints.json` (артефакт)
  - Required changes: `endpoints.py` с чистыми парсерами + `fetch_endpoints`; блок `endpoints` в filter_config; шаг в run_all; проброс ключа; тесты
  - Preserve: форма `data/routerai_models_raw.json`/`data/meta.json`; атомарность записи; отсутствие `--apply`-зависимости; существующие шаги конвейера и их порядок
  - Out of scope: `sync_configs.py`, `filter_models.py`, `build_report.py`, `recommend.py`, ai-core (весь)
  - Verification: `pytest tests/test_endpoints.py -q` зелёный; `data/endpoints.json` содержит `deepseek/deepseek-v4.1-flash` c `tag=deepseek`; `run_all.py --no-fetch` exit 0
  - Stop conditions: нет `ROUTERAI_API_KEY` (env и `.env`) | API endpoint'ов недоступен | требуется менять форму каталога моделей | требуется трогать ai-core

- **Phase 2**
  - Base revision: HEAD после Phase 1
  - Allowed files: `sync_configs.py`, `filter_models.py`, `build_report.py`, `compare_with_config.py`, `filter_config.json`, `tests/test_policy.py` (new), `tests/test_source.py`, `tests/test_filtering.py`, `README.md`
  - Required changes: `_meta.routes`; `provider_policy` + `pricing_overrides` в config.json; валидация `order` по endpoint'ам; sticky-сохранение; дифф в compare
  - Preserve: форма `models[]`; `maxInput/MaxOutputTokens` по тирам; агрегатный `cost_per_turn_estimate_rub` и тиры (102/38/158-подобное распределение не обязано совпадать, но формулы не менять); защита записи (`check_catalog_ok`); без `--apply` config.json неизменен
  - Out of scope: `chatLanguageModels.json`/`build_clm_provider`; `endpoints.py` (только чтение); ai-core (весь)
  - Verification: `pytest -q` зелёный; dry-run печатает непустой дифф политики/цен; sha256 config.json ai-core до==после dry-run
  - Stop conditions: `order[0]` отсутствует у отобранных моделей | требуется менять схему clm | требуется трогать ai-core

- **Phase 3**
  - Base revision: ai-core `2f74a6bc` (ре-верифицировать HEAD перед началом)
  - Allowed files: `core/config_manager.py`, `core/llm_engine.py`, `core/cost_estimator.py`, `main.py`, `config/config.example.json`, `tests/test_provider_policy.py` (new), `tests/test_cost_overrides.py` (new)
  - Required changes: поля политики в `LLMProviderConfig`; сборка `extra_body.provider` в `LLMEngine` под гейтом `provider=="routerai"`; `CostEstimator.load_overrides`; инициализация overrides при старте
  - Preserve: `OpenAICompatibleProvider` и его `payload.update`; каскад `get_pricing`; поведение при пустой политике (тело запроса прежнее); `_setup_providers`; форма `models[]`; отсутствие влияния на deepseek/yandex
  - Out of scope: роутер-модули (telemetry/server.py), web-UI планы, agency-планы, `config/config.json` (живой, только генератор его пишет)
  - Verification: `./venv/bin/python -m pytest tests/test_provider_policy.py tests/test_cost_overrides.py tests/test_openai_provider_retry.py -q` зелёный; `ruff` по изменённым файлам чисто
  - Stop conditions: `extra_body` уже занят другим потребителем и конфликтует | требуется менять публичную сигнатуру `generate_response` | требуется трогать файлы вне списка | параллельный план правит те же строки

- **Phase 4**
  - Base revision: HEAD после Phase 3 (оба репо)
  - Allowed files: интеграционные тесты (new), `README.md` оба репо, `config/config.example.json`
  - Required changes: интеграционные тесты «конфиг→запрос→цена» и «каталог→config.json»; README
  - Preserve: всё из Phase 1–3
  - Out of scope: любые продуктовые правки кода
  - Verification: интеграционные тесты зелёные; `run_all.py` exit 0; двойной `--apply` идемпотентен
  - Stop conditions: `run_all.py` не зелёный | идемпотентность не достигается

**Further Considerations** (ожидают подтверждения пользователя)

1. **Уровень `route_min` в бюджете.** Сейчас минимум route-цены — оптимистичная оценка; реальный факт зависит от выбранного endpoint. Если нужен гарантированный бюджет — имеет смысл поднять `provider_policy.order` с одним тегом для дорогих ролей (тогда `pricing_basis="pinned"` и цена детерминирована). Подтвердить перед Phase 2.
2. **Регламент пересмотра `order`.** Теги endpoint'ов и их доступность меняются; при исчезновении `order[0]` политика должна деградировать (fallback), а не ломать запросы. Подтвердить поведение при 404 от pin.
3. **Метрика проверки на реальных задачах.** Фиксация провайдера не устраняет циклы генерации — отдельно решить, нужен ли A/B-прогон агентного цикла по endpoint'ам (вне объёма плана).
