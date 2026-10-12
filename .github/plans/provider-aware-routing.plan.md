## Plan: Provider-aware routing & route pricing

**Progress State** (single source of truth for resumption — update in place, never append history)
- **Plan file**: `.github/plans/provider-aware-routing.plan.md` (репо `routerai-models`) — канонический файл, который обновляет Enagent; MEMORY INDEX НЕ ЗАМЕНА
- **Memory index**: Registered (`/memories/repo/plan-index.md`)
- **Status**: In progress — updated 2026-10-12
- **Base revision**: routerai-models `b837ed1f658e615fab6fbe0d4f62d1b58bf8dafc` on branch `main`; ai-core `9422df313e6f1c53bcaa3e265cec16799415df07` on branch `main`. Если HEAD Enagent отличается — сначала переверить план по текущему репо (особенно `sync_configs.py`, `core/llm_engine.py`, `core/config_manager.py`)
- **Current phase**: Phase 4 — Depends on Phase 3 completion (ai-core integration complete)
- **Completed**: 
  - ✅ `endpoints.py` created with all parsing functions
  - ✅ `filter_config.json` has `endpoints` block with trailing comma fixed
  - ✅ `run_all.py` has `fetch_endpoints` step with `NETWORK_STEPS = {0, 4}`
  - ✅ `data/endpoints.json` generated (47 models, 215 endpoints)
  - ✅ `tests/test_endpoints.py` created with 33 tests covering: parsing, rub_per_1m conversion, cheapest_endpoint, route_vs_aggregate, atomic write, missing model → empty list, API shape variants
  - ✅ `pytest tests/test_endpoints.py -q` — 33 passed
  - ✅ `python3 run_all.py --no-fetch` — exits 0
- **Last verification**: pytest tests/test_endpoints.py -q (33 passed), python3 run_all.py --no-fetch (exit 0)
- **Next incomplete phase**: Phase 4 — Depends on Phase 3 completion (ai-core integration complete)
- **Phase dependencies**: Phase 2 ← Phase 1 (артефакт `data/endpoints.json`); Phase 3 ← Phase 2 (артефакты `provider_policy` и `pricing_overrides` в config.json); Phase 4 ← Phase 3
- **Blockers / open unknowns**: `ROUTERAI_API_KEY` есть в `routerai-models/.env`; `fetch_models.py` читает его из env-окружения (`os.environ`). systemd-юит (5 отдельных ExecStart=) не задаёт переменные — для `run_all.py` как entrypoint потребуется добавить Environment= или env_file, но это не блокирует Phase 1. ai-core параллельно правят другие планы (`agency-concurrency-races-fix` Phase 4 идёт; `web-chat-sessions-roles-models` не начат) — Phase 3 трогает те же файлы, координировать порядок.

RouterAI различает **модель** (`deepseek/deepseek-v4.1-flash`) и **провайдера** — конкретный endpoint этой модели. Без параметра `provider` шлюз сам выбирает маршрут и тарифицирует **фактически использованный** endpoint, а не минимальную цену модели в каталоге. Для одной модели разброс цен кратен: проверено живьём для `deepseek/deepseek-v4.1-flash` — 30 endpoint'ов, `tag=deepseek` = 18.2 ₽/1M (prompt) против агрегированной цены каталога 4.22 ₽/1M. План добавляет второй уровень данных (endpoint'ы), второй уровень цены (цена маршрута) и политику выбора провайдера, доведённую до тела API-запроса и до учёта затрат.

**Context & Current State**

_Проверенные факты (не предположения):_

- **API endpoint'ов существует и подтверждён живьём.** `GET https://routerai.ru/api/v1/models/{model_id}/endpoints` → `{"data": {"id", "name", "architecture", "created", "description", "endpoints": [...]}}`. Каждый endpoint: `tag`, `provider_name`, `name`, `status` (0 = ok, <0 = деприоритизирован), `quantization`, `countries`/`country`, `context_length`, `max_completion_tokens`, `max_prompt_tokens`, `supported_parameters` (содержит `tools`/`tool_choice`/`reasoning`), `supported_apis` (chat, messages, responses, embeddings, audio, media), `pricing.{prompt,completion,input_cache_read,image_output,seconds}` (₽ по ключам), `pricing_units.{key}` (token/request/image/megapixel/second/search_unit), `variable_pricings[]` (type=prompt-threshold, threshold, price). У `deepseek/deepseek-v4.1-flash` вернулось 30 endpoint'ов, `endpoints[0].tag == "deepseek"`. **Авторизация НЕ требуется** — документация RouterAI прямо: «Авторизация не требуется».
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
   - Новый модуль `endpoints.py`: чистые функции — `parse_endpoints(payload) -> list[Endpoint]` (нормализует один endpoint: `tag`, `provider_name`, `name`, `status` (0 = ok, <0 = деприоритизирован), `quantization`, `countries`/`country`, `context_length`, `max_completion_tokens`, `max_prompt_tokens`, `supported_apis` (chat, messages, responses, embeddings, audio, media), `supported_parameters` (tools, tool_choice, reasoning и т.д.), `pricing.{prompt,completion,input_cache_read,image_output,seconds,search_unit}` (₽ по ключам), `pricing_units.{key}` (token/request/image/megapixel/second/search_unit), `variable_pricings[]` (type=prompt-threshold, threshold, price); **конвертация единиц:** `rub_per_1m.{key} = pricing[key] × 1e6` ТОЛЬКО если `pricing_units[key] == "token"`; для других единиц (`request`, `image`, `megapixel`, `second`, `search_unit`) цена остаётся в исходной единице и НЕ участвует в токенных сравнениях; флаг `supports_tools` = `"tools" in supported_parameters`; функция `is_token_endpoint(eps)` фильтрует endpoint'ы с `chat` в `supported_apis`, `tools` в `supported_parameters`, достаточными лимитами); **стоимость типового вызова** (cost_per_turn): `C = (Np × Pp + Nc × Pc) / 1e6`, где Np/Nc — количество входных/выходных токенов из `call_params` filter_config, Pp/Pc — `rub_per_1m_prompt`/`rub_per_1m_completion`; `endpoint_rub_per_1m(ep)` — только для токеновых ключей; `cheapest_endpoint(eps)` — не просто min `prompt`, а минимальная cost_per_turn среди endpoint'ов с `chat`+`tools` (считается по формуле выше); `endpoints_with_tools(eps)`; `route_vs_aggregate(aggregate_cost_per_turn, eps)` — агрегатную цену тоже преобразовать в cost_per_turn через те же `call_params`, затем вычислять процентную дельту.
   - Сетевой слой `fetch_endpoints(model_ids, cfg, api_key) -> dict[str, list[Endpoint]]`: `GET {base_url}/models/{id}/endpoints`, атомарная запись `data/endpoints.json` (tmp+replace, как `fetch_models.save_raw_data`). Ретраи/таймаут как в `fetch_models`.
   - **Ограничить объём выгрузки**: эндпоинты тянуть не для всех 559 моделей, а для множества из `filter_config.endpoints.scope`. Источник модели для scope — результат `recommend`/`sync`: `selected` (топ-15 из отбора по цене/тиру), `reference` (эталонная модель из filter_config), `asr` (ASR-модели, отделённые от chat). Обоснование: 559 последовательных вызовов недопустимы; политика и route-цены нужны только для моделей, реально попадающих в config.json. Если scope не задан — фолбэк на selected ∪ reference ∪ asr.
   - **Авторизация endpoint'ов НЕ требуется.** Документация RouterAI прямо: «Авторизация не требуется». Ключ `ROUTERAI_API_KEY` нужен только для `/chat/completions`, но не для `/models/{author}/{model}/endpoints`. Проброс ключа в systemd и `.env`-plumbing **НЕ входит** в фазу.
   - `filter_config.json`: новый блок `endpoints` (`scope`, `timeout_seconds`, `stale_after_hours`, `min_endpoints_per_model`).
   - `run_all.py`: новый шаг `fetch_endpoints` (после `recommend`, до `sync_configs`). **Важно:** `--no-fetch` пропускает только первый шаг конвейера (step 0). Поскольку `fetch_endpoints` будет шагом N > 0, он запустится всегда. Решение: определить `NETWORK_STEPS = [0, ...]` и проверять `args.no_fetch` против индекса шага; либо сделать fetch_endpoints optional-step с graceful degradation на stale endpoints.json.
   - **systemd интеграция:** текущий service содержит 5 отдельных ExecStart= (каждый .py скрипт отдельно), `run_all.py` не используется. Добавление шага в `run_all.py` не активируется по расписанию. Два варианта: (a) заменить 5 ExecStart на один `python3 run_all.py` + добавить Environment=ROUTERAI_API_KEY; (b) оставить 5 скриптов, но обеспечить что `fetch_endpoints` запускается вручную или через отдельный timer. **Рекомендуется вариант (a)** — централизация конвейера. Это изменение вне scope Phase 1, но задокументировать в README.
   - `fetch_models.save_raw_data`/`meta` дополнить отметкой актуальальности endpoint-каталога.
   - Tests: `tests/test_endpoints.py` (парсинг формы ответа, `supports_tools` по `supported_parameters`, `rub_per_1m` из `pricing`, `cheapest_endpoint`, `route_vs_aggregate`, атомарность записи, отсутствие модели → пустой список, а не падение).
   - **Verification**: `pytest tests/test_endpoints.py -q` → все зелёные; `python3 -c` проверка `data/endpoints.json` содержит `deepseek/deepseek-v4.1-flash` с ≥1 endpoint и `tag=deepseek`; `run_all.py --no-fetch` не падает на новом шаге.

2. **Phase 2 — Route-цены и provider-политика в каталоге и синхронизаторе (routerai-models).** _depends on Phase 1._
   - `build_report.py`/`filter_models.py`: к `_meta` модели добавить `routes: {n, tools, min_cost_per_turn_estimate_rub, median_cost_per_turn_estimate_rub, max_cost_per_turn_estimate_rub, cheapest_tag, aggregate_minus_cheapest_percent}` — считать из `data/endpoints.json`, фолбэк на пустой блок, если endpoint'ов нет. `cost_per_turn_estimate_rub` (агрегатный) НЕ переопределять — добавить рядом `route_cost_per_turn_estimate_rub`. **Важно:** cost_per_turn считается только для endpoint'ов с `chat` в `supported_apis` и `tools` в `supported_parameters` (для агентских вызовов), используя `call_params` из filter_config.
   - `sync_configs.py` — новые записи:
     - `llm.providers.routerai.provider_policy` (из `filter_config.provider_policy`): **per-model структура** вместо единого глобального `order`:
       ```python
       {
           "mode": "per_model",  # или "global" если нужен единый order
           "models": {
               "deepseek/deepseek-v4.1-flash": {"order": ["deepseek", "yandex"], "allow_fallbacks": true},
               "anthropic/claude-sonnet-4.5": {"order": ["anthropic", "google"]},
               ...
           },
           "default": {"order": [], "allow_fallbacks": true}  # fallback для моделей без записи
       }
       ```
       Каждая модель проверяет свой `order` только против своих endpoint'ов из `data/endpoints.json`. Если `order[0]` отсутствует у модели — эта запись пропускается, используется `default`. Глобальный `order` отклонён: разные семейства моделей имеют разных провайдеров (DeepSeek-тег не применим к Google/Qwen).
     - `llm.providers.routerai.pricing_overrides`: `{ "<full model id>": {prompt_token, completion_token} }` — ₽/токен. Источник: если для модели пинован `preferred_tag` (из `filter_config.provider_policy.per_model` или гейта роли) — цена этого endpoint'а; иначе — **минимальная** route-цена (как оптимистичная оценка), с записью `_meta.pricing_basis: "route_min" | "preferred"`. Документировать, что `route_min` — не гарантированная фактическая стоимость, а нижняя граница бюджета. RouterAI тарифицирует фактический endpoint, который может отличаться от минимума. **Важно:** `pricing_overrides` содержит ТОЛЬКО базовые токенные тарифы (`prompt_token`, `completion_token`). Переменные пороги из `variable_pricings` НЕ включаются — CostEstimator v1 не поддерживает пороговые тарифы. Модели с `variable_pricings` помечаются как приблизительные оценки. Не называть `route_min` «нижней границей бюджета» — это оптимистичная оценка тарифа, а не гарантия максимальных расходов.
     - «Липкость»: существующие `provider_policy`/`pricing_overrides` в config.json при `--apply` сохранять, если `filter_config` их не задаёт (тот же паттерн, что Phase 4 плана `routerai-models-catalog-reliability`).
   - `compare_with_config.py`: отчёт по расхождению `provider_policy`/`pricing_overrides` (новые/исчезнувшие/изменившиеся записи) — отдельным блоком, не ломая текущий вывод.
   - Tests: `tests/test_policy.py` (policy формируется только при валидном order; route-цена в ₽/токен = route_rub/1e6; sticky-поведение; `_meta.routes` в `catalog.json`); расширить `tests/test_source.py` на форму `endpoints.json`.
   - **Verification**: `pytest -q` → зелёные (существующие + новые); `python3 sync_configs.py` (dry-run) показывает непустой дифф `provider_policy`/`pricing_overrides`; `sha256 config/config.json` ai-core ДО == ПОСЛЕ без `--apply` (инвариант защиты записи).

3. **Phase 3 — Потребление в ai-core: политика в теле запроса + route-цена в учёте затрат.** _depends on Phase 2 (артефакты в config.json)._
   - `core/config_manager.py`: `LLMProviderConfig` (строка ~378, dataclass) дополнить двумя новыми полями:
     ```python
     provider_policy: Dict[str, Any] = field(default_factory=dict)  # {order, allow_fallbacks, policy}
     pricing_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)  # model_id → {prompt_token, completion_token}
     ```
     В загрузке конфига (~строка 1460–1475, где создаётся `LLMProviderConfig(models=...)`) экстрактить эти ключи из JSON:
     ```python
     LLMProviderConfig(
         models=pdata.get('models', []),
         provider_policy=pdata.get('provider_policy', {}),
         pricing_overrides=pdata.get('pricing_overrides', {}),
     )
     ```
     `LLMConfigWrapper` (main.py:3264) — добавить два атрибута после блока `providers = config_manager.llm_config.providers`:
     ```python
     self.routerai_provider_policy = providers.get('routerai', LLMProviderConfig()).provider_policy
     self.routerai_pricing_overrides = providers.get('routerai', LLMProviderConfig()).pricing_overrides
     ```
   - `core/llm_engine.py`: в `LLMEngine.generate_response` (def ~1361) перед вызовом провайдера — если итоговый `provider == "routerai"` и `self.config.routerai_provider_policy` непустая, **извлечь per-model политику**:
     ```python
     # model_id = "deepseek/deepseek-v4.1-flash" (хвост от routerai:...)
     policy = self.config.routerai_provider_policy.get("models", {}).get(model_id, {})
     if not policy:
         policy = self.config.routerai_provider_policy.get("default", {})
     if policy:
         order = policy.get("order", [])
         allow_fallbacks = policy.get("allow_fallbacks", True)
         policy_body = {"provider": {"order": order, "allow_fallbacks": allow_fallbacks}}
     ```
     Порядок слияния `extra_body` (критично): сначала политика → потом вызывающий перебивает:
     ```python
     merged_extra = {**policy_body, **(caller_extra or {})}
     ```
     Это гарантирует, что явный `extra_body.provider` вызывающего выигрывает при конфликте. Политика не добавляется ни для `deepseek`, ни для `yandex` — гейт `provider == "routerai"`. Провайдер-класс не менять — он уже делает `payload.update`.
   - `core/cost_estimator.py`: добавить `CostEstimator.load_overrides(dict)` (слияние в `model_overrides`, совместимо с `get_pricing`); сохранить fallback-каскад `model_id → overrides → short → default`. `get_cost_estimator()` не ломать (синглтон без overrides для тестов). **Важно:** `pricing_overrides` содержит ТОЛЬКО базовые токенные тарифы (`prompt_token`, `completion_token`). Переменные пороги из `variable_pricings` НЕ поддерживаются в CostEstimator v1 — модели с `variable_pricings` помечаются как приблизительные оценки.
   - Точка инициализации: при старте (main.py, рядом с созданием `ConfigManager`/`LLMConfigWrapper`) вызвать `get_cost_estimator().load_overrides(config_manager.llm_config.providers['routerai'].pricing_overrides)` — с `try/except` (graceful degradation, как у `usage_callback`). Не перезаписывать существующие CU-цены статики; overrides только для id, пришедших из каталога.
   - Tests: `tests/test_provider_policy.py` — (a) payload routerai-запроса содержит `provider.order`/`allow_fallbacks`; (b) для `deepseek:`/`yandex` поля нет; (c) `extra_body` вызывающего переопределяет политику; (d) пустая политика → поведение прежнее. `tests/test_cost_overrides.py` — (a) override применяется к CCU-расчёту; (b) отсутствующий id → `default` + warning; (c) `load_overrides` не портит статику.
   - **Verification**: `./venv/bin/python -m pytest tests/test_provider_policy.py tests/test_cost_overrides.py -q` → зелёные; регрессионный прогон `./venv/bin/python -m pytest tests/test_openai_provider_retry.py -q` → зелёный (тело запроса не сломано); `ruff` чисто по изменённым файлам.

4. **Phase 4 — Сквозная проверка, документация и systemd.** _depends on Phase 3._
   - Интеграционный тест «конфиг → тело запроса → цена»: с фиктивным `ConfigManager` с `provider_policy` и `pricing_overrides`, проверить, что `LLMEngine.generate_response("routerai:...")` кладёт `provider` в payload (мок httpx, как в `test_openai_provider_retry.py`) и что `CostEstimator` считает по route-цене, а не по `DEFAULT_PRICING`.
   - Проверка «каталог → config.json»: прогон `run_all.py` в dry-run, затем `--apply` в **копию** config.json (не в живой) — подтвердить идемпотентность (второй прогон не меняет файл).
   - **systemd интеграция:** заменить 5 ExecStart= на единый `ExecStart=/usr/bin/python3 /home/dev/projects/routerai-models/run_all.py` + добавить `EnvironmentFile=/home/dev/projects/routerai-models/.env`; либо оставить 5 скриптов и добавить отдельный `.service`/`.timer` для `fetch_endpoints`. Документировать выбор.
   - **Stale endpoint check:** добавить проверку свежести `data/endpoints.json` перед запуском конвейера (`stale_after_hours` из filter_config); при устаревании — warning или abort (configurable). Тест: симуляция stale endpoints.json → graceful degradation.
   - README обоих репо: раздел про provider-политику, route-цены, ограничение `route_min` (оценка ≠ факт), как получить `ROUTERAI_API_KEY`, как закрепить endpoint (`order` c одним tag), systemd setup.
   - **Verification**: оба интеграционных теста зелёные; `run_all.py` exit 0; `compare_with_config.py` без ошибок; прогон `--apply` в копию дважды → sha256 второго прогона == первого; systemd unit reload + start без ошибок; stale endpoints.json → warning в логе.

**Relevant Files & Symbols**

- `routerai-models/endpoints.py` — Create. Парсеры endpoint'ов (`parse_endpoints`, `cheapest_endpoint`, `endpoints_with_tools`, `route_vs_aggregate`) + `fetch_endpoints()` с атомарной записью `data/endpoints.json`.
- `routerai-models/filter_config.json` — Modify. Новые блоки `endpoints` (`scope`, `stale_after_hours`, `min_endpoints_per_model`) и `provider_policy` (`per_model`).
- `routerai-models/run_all.py` — Modify `STEPS`/`main`: новый шаг `fetch_endpoints` между `recommend` и `sync_configs`.
- `routerai-models/filter_models.py` / `build_report.py` — Modify: `_meta.routes` и `route_cost_per_turn_estimate_rub` в `data/catalog.json`/`config_models.json`.
- `routerai-models/sync_configs.py` — Modify `to_ai_core_model` (форма записи модели не меняется), новые писатели `provider_policy`/`pricing_overrides`, sticky-сохранение; точка входа `update_ai_core_config` (571).
- `routerai-models/compare_with_config.py` — Modify: блок диффа политики/цен.
- `routerai-models/tests/test_endpoints.py`, `tests/test_policy.py` — Create; `tests/test_source.py`, `tests/test_filtering.py` — Modify.
- `routerai-models/systemd/routerai-models-fetch.service` — Modify (Phase 4): заменить 5 ExecStart на единый entrypoint или добавить отдельный timer для fetch_endpoints.
- `ai-core/core/config_manager.py` — Modify `LLMProviderConfig` (строка ~378, dataclass): добавить `provider_policy` + `pricing_overrides`; загрузка конфига (~1460–1475) экстрактит их из JSON при создании `LLMProviderConfig`. `merge_catalog_models_into_catalog` (315) — Reuse без изменений формы.
- `ai-core/core/llm_engine.py` — Modify `LLMEngine.generate_response` (def ~1361): сборка `policy_body = {"provider": {...}}` под гейтом `provider=="routerai"`; слияние `{**policy_body, **(caller_extra or {})}` чтобы вызывающий перебивал политику. `OpenAICompatibleProvider` (класс на 522, метод на 549) — Reuse без изменений.
- `ai-core/core/cost_estimator.py` — Modify `CostEstimator`: добавить `load_overrides()`; `get_cost_estimator` (291) — Reuse.
- `ai-core/main.py` — Modify `LLMConfigWrapper` (3264): добавить `self.routerai_provider_policy` + `self.routerai_pricing_overrides`; инициализация overrides при старте.
- `ai-core/tests/test_provider_policy.py`, `tests/test_cost_overrides.py` — Create.
- `ai-core/config/config.example.json` — Modify: примеры `provider_policy`/`pricing_overrides` (без секретов).

**Verification**

1. Phase 1: `cd routerai-models && pytest tests/test_endpoints.py -q` → все зелёные; `data/endpoints.json` содержит `deepseek/deepseek-v4.1-flash` с `tag="deepseek"` в списке.
2. Phase 2: `cd routerai-models && pytest -q` → все зелёные (старые + новые); `python3 sync_configs.py` (dry-run) печатает непустой дифф `provider_policy`/`pricing_overrides`; `sha256sum /home/dev/projects/ai-core/config/config.json` не меняется после dry-run.
3. Phase 3: `cd ai-core && ./venv/bin/python -m pytest tests/test_provider_policy.py tests/test_cost_overrides.py tests/test_openai_provider_retry.py -q` → все зелёные; `./venv/bin/ruff check core/llm_engine.py core/cost_estimator.py core/config_manager.py main.py` → чисто.
4. Phase 4: интеграционные тесты зелёные; `cd routerai-models && python3 run_all.py --no-fetch` → exit 0; двойной `--apply` в копию config.json → sha256 не меняется между прогонами.

**Risks & Rollback**

- **Политика ломает не-routerai провайдеры / 400 от API.** Смягчение: гейт по `provider == "routerai"` + тест (b) в Phase 3; `allow_fallbacks: true` по умолчанию. Откат: убрать `provider_policy` из config.json — код молча вернётся к прежнему телу запроса (пустая политика = no-op).
- **Единый global order невалиден для mixed-model set.** Разные семейства моделей имеют разных доступных провайдеров (DeepSeek-тег не применим к Google/Qwen). Решение: per-model структура `{models: {model_id: {...}}, default: {...}}`, каждая модель валидирует свой order только против своих endpoint'ов.
- **Route-цены «route_min» выдаются за факт.** Это оптимистичная оценка: шлюз тарифицирует фактический endpoint, который может отличаться от минимума (stability→price→fallback алгоритм). Смягчение: `_meta.pricing_basis`, README-предупреждение, `provider_policy.order` для контроля маршрута. Откат: удалить `pricing_overrides` — CostEstimator вернётся к `DEFAULT_PRICING`.
- **559 сетевых вызовов / rate-limit.** Смягчение: `endpoints.scope = selected + reference + asr`, таймауты и `min_endpoints_per_model`; сбой выгрузки эндпоинтов не блокирует конвейер (фолбэк на пустые `routes`).
- **systemd не использует run_all.py.** Текущий service содержит 5 отдельных ExecStart= скриптов. Добавление шага в `run_all.py` не активируется по расписанию. Решение: задокументировать необходимость перехода на единый entrypoint или отдельный timer для fetch_endpoints. Не блокирует Phase 1/2.
- **Конкурентные правки ai-core.** Другие активные планы трогают `core/llm_engine.py`/`config_manager.py`. Смягчение: Phase 3 делать после ре-верификации HEAD; конфликты решать в пользу существующих планов.
- **Дрейф кэша CLM/VS Code.** Новые поля в `build_clm_provider` не добавлять (VS Code clm не знает `provider`), иначе ломается схема `chatLanguageModels.json`. Смягчение: политику писать **только** в ai-core config.json.
- Rollback фаз: routerai-models — `git revert` коммита фазы (данные в `data/` в `.gitignore`, восстанавливаются прогоном); ai-core — `git revert` + удаление `provider_policy`/`pricing_overrides` из config.json.

**Decisions & Scope Boundaries**

- **Included**: endpoint-каталог и route-цены в `routerai-models`; запись `provider_policy` + `pricing_overrides` в ai-core config.json генератором; чтение политики и проброс её в тело RouterAI-запроса в ai-core; route-цены в `CostEstimator`; тесты обоих репо; документация.
- **Excluded**: изменение тиринга (cheap/medium/premium) и `cost_per_turn_estimate_rub`; правки `chatLanguageModels.json`-схемы; UI/дашборды; авто-выбор провайдера «обучением» (эвристика по совместимости вне объёма); `@tag`-пин в строке модели (механизм B отклонён); изменение самих провайдер-классов `DeepSeekProvider`/`YandexGPTProvider`.
- **Key Decisions**: механизм политики — `extra_body.provider = {order, allow_fallbacks}` (A); хранение — глобальный `llm.providers.routerai.provider_policy` + `pricing_overrides` (A); охват — сквозной, оба репо (A); эндпоинты грузятся только для отобранных моделей.

**Decision Record**

- **Механизм передачи политики**: Problem — политику нужно доставить в тело API, но провайдер-класс общий. Evidence — `payload.update(extra_body)` уже есть (llm_engine.py:626), вызовов `extra_body` нет. Options — A `extra_body.provider{order,allow_fallbacks}`; B суффикс `@tag`; C оба. Selected — A. Why — структурированный список предпочтений + резерв, авто-резервирование сохраняется, точка вставки существует, сравнение моделей не ломается. Rejected — B: абсолютная привязка без fallback, 404 при несовместимости, не выразить список.
- **Где жить политике**: Problem — 15 отобранных моделей + роли. Evidence — `LLMConfig.model_overrides` (роль→модель) уже есть; `LLMProviderConfig` не имеет полей политики. Options — A глобальный блок на провайдера + per-role overrides; B поле в каждой записи модели; C отдельный файл. Selected — per-model структура `{models: {model_id: {order, allow_fallbacks}}, default: {...}}`. Why — разные семейства моделей имеют разных доступных провайдеров (DeepSeek-тег не применим к Google/Qwen), единый order сломается на первой несовместимой модели. Rejected — единый global order: невалиден для mixed-model set.
- **Единицы route-цены**: Problem — CostEstimator ждёт ₽/токен, каталог даёт ₽/1M. Evidence — `PricingTier.prompt_token` vs `filter_models.rub_per_1m = ×1e6`. Options — писать ₽/1M и конвертировать в коде; писать ₽/токен. Selected — ₽/токен в `pricing_overrides` (как ждёт `get_pricing`). Why — нулевая конвертация на рантайме, нет риска двойного ×1e6. Rejected — ₽/1M: конвертацию пришлось бы помнить в каждом потребителе.
- **Источник цены маршрута при нескольких endpoint'ах**: Problem — какой endpoint считать. Evidence — 30 endpoint'ов у одной модели, разброс 4.22→390 ₽/1M. Options — минимальная route-цена; цена preferred-тега; средняя. Selected — preferred-тег, если задан, иначе минимум с флагом `pricing_basis="route_min"`. Why — предсказуемо и объяснимо; минимум = оптимистичная оценка тарифа (не гарантия расходов). Rejected — средняя: размывает и не отражает ни один реальный маршрут. **Важно:** `route_min` — оптимистичная оценка тарифа, а не «нижняя граница бюджета» и не гарантия максимальных расходов. При `allow_fallbacks: true` RouterAI может использовать другой endpoint с иной ценой.

**Implementation Contract** (per phase — Enagent's binding sandbox)

- **Phase 1**
  - Base revision: routerai-models `b837ed1f658e615fab6fbe0d4f62d1b58bf8dafc`
  - Allowed files: `endpoints.py` (new), `filter_config.json`, `run_all.py`, `tests/test_endpoints.py` (new), `README.md`, `data/endpoints.json` (артефакт)
  - Required changes: `endpoints.py` с чистыми парсерами + `fetch_endpoints`; блок `endpoints` в filter_config; шаг в run_all с обработкой `--no-fetch` (optional-step); тесты
  - Preserve: форма `data/routerai_models_raw.json`/`data/meta.json`; атомарность записи; отсутствие `--apply`-зависимости; существующие шаги конвейера и их порядок
  - Out of scope: systemd service (не менять, только задокументировать в README), `sync_configs.py`, `filter_models.py`, `build_report.py`, `recommend.py`, `fetch_models.py`, ai-core (весь), проброс `ROUTERAI_API_KEY` (endpoint'ы не требуют авторизации)
  - Verification: `pytest tests/test_endpoints.py -q` зелёный; `data/endpoints.json` содержит `deepseek/deepseek-v4.1-flash` c `tag=deepseek`; `run_all.py --no-fetch` exit 0
  - Stop conditions: API endpoint'ов недоступен | требуется менять форму каталога моделей | требуется трогать ai-core

- **Phase 2**
  - Base revision: HEAD после Phase 1
  - Allowed files: `sync_configs.py`, `filter_models.py`, `build_report.py`, `compare_with_config.py`, `filter_config.json`, `tests/test_policy.py` (new), `tests/test_source.py`, `tests/test_filtering.py`, `README.md`
  - Required changes: `_meta.routes`; `provider_policy` в per-model структуре `{models: {...}, default: {...}}`; `pricing_overrides` с флагом `pricing_basis`; валидация order по endpoint'ам каждой модели отдельно; sticky-сохранение; дифф в compare
  - Preserve: форма `models[]`; `maxInput/MaxOutputTokens` по тирам; агрегатный `cost_per_turn_estimate_rub` и тиры; защита записи (`check_catalog_ok`); без `--apply` config.json неизменен
  - Out of scope: `chatLanguageModels.json`/`build_clm_provider`; `endpoints.py` (только чтение); ai-core (весь)
  - Verification: `pytest -q` зелёный; dry-run печатает непустой дифф политики/цен; sha256 config.json ai-core до==после dry-run
  - Stop conditions: требуется менять схему clm | требуется трогать ai-core

- **Phase 3**
  - Base revision: ai-core `9422df313e6f1c53bcaa3e265cec16799415df07` (ре-верифицировать HEAD перед началом)
  - Allowed files: `core/config_manager.py`, `core/llm_engine.py`, `core/cost_estimator.py`, `main.py`, `config/config.example.json`, `tests/test_provider_policy.py` (new), `tests/test_cost_overrides.py` (new)
  - Required changes:
    - `LLMProviderConfig` (dataclass ~378): добавить `provider_policy: Dict[str, Any] = field(default_factory=dict)` + `pricing_overrides: Dict[str, Dict[str, float]] = field(default_factory=dict)`; загрузка конфига (~1460–1475) экстрактит их из JSON при создании `LLMProviderConfig(models=..., provider_policy=..., pricing_overrides=...)`.
    - `LLMConfigWrapper` (3264): добавить `self.routerai_provider_policy` + `self.routerai_pricing_overrides`.
    - `LLMEngine.generate_response` (def ~1361): сборка `policy_body = {"provider": {"order": ..., "allow_fallbacks": ...}}`; слияние `{**policy_body, **(caller_extra or {})}` — вызывающий перебивает политику; гейт `provider == "routerai"`.
    - `CostEstimator`: добавить `load_overrides(dict)` (слияние в `model_overrides`).
    - main.py: инициализация overrides при старте через `get_cost_estimator().load_overrides(...)`.
  - Preserve: `OpenAICompatibleProvider` и его `payload.update`; каскад `get_pricing`; поведение при пустой политике (тело запроса прежнее); `_setup_providers`; форма `models[]`; отсутствие влияния на deepseek/yandex
  - Out of scope: роутер-модули (telemetry/server.py), web-UI планы, agency-планы, `config/config.json` (живой, только генератор его пишет)
  - Verification: `./venv/bin/python -m pytest tests/test_provider_policy.py tests/test_cost_overrides.py tests/test_openai_provider_retry.py -q` зелёный; `ruff` по изменённым файлам чисто
  - Stop conditions: `extra_body` уже занят другим потребителем и конфликтует | требуется менять публичную сигнатуру `generate_response` | требуется трогать файлы вне списка | параллельный план правит те же строки

- **Phase 4**
  - Base revision: HEAD после Phase 3 (оба репо)
  - Allowed files: интеграционные тесты (new), `README.md` оба репо, `config/config.example.json`, `systemd/routerai-models-fetch.service` (modify)
  - Required changes: интеграционные тесты «конфиг→запрос→цена» и «каталог→config.json»; README; systemd unit — заменить 5 ExecStart на единый entrypoint или добавить отдельный timer для fetch_endpoints; stale endpoint check + graceful degradation
  - Preserve: всё из Phase 1–3
  - Out of scope: любые продуктовые правки кода
  - Verification: интеграционные тесты зелёные; `run_all.py` exit 0; двойной `--apply` идемпотентен; systemd unit reload + start без ошибок; stale endpoints.json → warning в логе
  - Stop conditions: `run_all.py` не зелёный | идемпотентность не достигается | systemd unit не стартует
  - Stop conditions: `run_all.py` не зелёный | идемпотентность не достигается

**Further Considerations** (ожидают подтверждения пользователя)

1. **Уровень `route_min` в бюджете.** Сейчас минимум route-цены — оптимистичная оценка тарифа; реальный факт зависит от выбранного endpoint RouterAI (stability→price→fallback алгоритм). Если нужен гарантированный бюджет — имеет смысл поднять `provider_policy.order` с одним тегом для дорогих ролей (тогда `pricing_basis="preferred"` и цена детерминирована). Подтвердить перед Phase 2.
2. **Регламент пересмотра `order`.** Теги endpoint'ов и их доступность меняются; при исчезновении `order[0]` политика должна деградировать (fallback), а не ломать запросы. Подтвердить поведение при 404 от pin.
3. **Метрика проверки на реальных задачах.** Фиксация провайдера не устраняет циклы генерации — отдельно решить, нужен ли A/B-прогон агентного цикла по endpoint'ам (вне объёма плана).
4. **variable_pricings support.** CostEstimator v1 не поддерживает пороговые тарифы. Для моделей с `variable_pricings` оценка будет приблизительной. Обсудить необходимость поддержки порогов в следующей итерации.
5. **systemd entrypoint.** Единый `run_all.py` vs 5 отдельных скриптов — выбор влияет на надёжность и отладку. Рекомендуется единый entrypoint с EnvironmentFile, но требует координации с другими планами, трогающими systemd.
