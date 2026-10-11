# Plan: RouterAI каталог — надёжность и синхронизация

**Progress State** (single source of truth for resumption — update in place, never append history)
- **Plan file**: `routerai-models/.github/plans/routerai-models-catalog-reliability.plan.md` — the canonical file Enagent updates; MEMORY INDEX IS NEVER A SUBSTITUTE
- **Memory index**: Registered (compact metadata entry in `/memories/repo/plan-index.md` — discovery only)
- **Status**: IN PROGRESS — Фаза 1 завершена (коммит `89e3a17`); Фаза 2 завершена (коммит `f9960b2`, 2026-10-11); Фаза 3 в работе
- **Base revision**: routerai-models `21197c1` → HEAD после Ф1 `89e3a17` (базовая линия зафиксирована); ai-core `663da8f` on branch `main` (ai-core НЕ изменяется в Фазах 1–6).
- **Current phase**: **Фаза 3 — P2: тиры, скоринг, рекомендации, гейт эталона**
- **Completed**: **Фаза 1** (коммит `89e3a17`, 3 файла baseline): `data/baseline/baseline-2026-10-11.json` (sha256 ai-core config/clm/imports, счётчики, резолв эталона, результат compare), `compare-before.json`, `issues.md`. Прогон `run_all.py` воспроизвёл баг 2 (эталон 0423 не найден, rc=1 на recommend); sha256 целевых конфигов до==после (инвариант без `--apply`). Замечен дрейф тиров между прогонами (101/38/159 → 102/38/158) — тесты не должны зависеть от абсолютных счётчиков. `data/` в .gitignore → baseline закоммичен через `git add -f`. Незакоммиченные `.gitignore` (newline) и `.github/` — не мои, не трогаю.

  **Фаза 2** (коммит `f9960b2`, 11 файлов): гейт `validation.min_valid_models` (50) и `min_valid_asr_models` (3) в `filter_config.json`; `filter_models.py` — гейты exit 2 (пустой/битый raw, каталог мал, ASR-гейт), проверка дубликатов id, non-token маркёры (`pricing_rub_per_1m: null`, `pricing_unit_warning`, `pricing_units`, `cost_per_turn_estimate_rub: null`), all-or-nothing запись (save только при all_ok), `--config`; `fetch_models.py` — атомарный `meta.json` (tmp+replace), `api_base_url`/`git_revision`/`schema_version`, проверка поля `data` (raise ValueError), Bearer `ROUTERAI_API_KEY`; `build_report.py` — hard-fail (exit 1) на битом recommendations, `--config`; `requirements-dev.txt` (pytest>=8.0), `pytest.ini` (marker live, addopts `-m "not live"`), `tests/conftest.py`, `tests/test_source.py` — 9 тестов, **9 passed**; README/.env.example — `AI_CORE_CONFIG_PATH`→`AI_CORE_CONFIG`, токен-лимиты 100K/80K/64K. Реальный прогон: 559 → 298 (102/38/158), ASR 22 (без цены 3), non_token_pricing=0, gate ok, exit 0. Verification: pytest зелёный; stub `{"data":[]}` → exit 2, filtered нетронут (тест); `run_all.py` exit 0 НЕВОЗМОЖЕН до Ф3 (recommend падает на эталоне 0423) — известное ограничение, снимается в Ф3.
- **Last verification**: pytest: 9 passed (tests/test_source.py). Прогон конвейера: fetch ok → filter ok (559→298, gate ok) → report ok → recommend FAIL (эталон 0423 не найден) → rc=1 (разблокируется в Ф3 авто-резолвом эталона).
- **Next incomplete phase**: **Фаза 3 — P2** → первое действие: `reliability.py` — `effective_reference_id(cfg, catalog)`, `is_model_approved(id, cfg)`, `reference_eligibility_notes(...)`; блок `reference_model` в конфиге (`id: null`, `approved_ids`, `watchlist`, `fallback`).
- **Phase dependencies**: Ф1 ни от чего не зависит; Ф2 ← Ф1; Ф3 ← Ф2; Ф4 ← Ф3; Ф5 ← Ф4; Ф6 (live-гейт) ← Ф5 (нужен `requirements-dev.txt`/pytest + окружение с ключом); Ф7 ← Ф6; применение записи в `ai-core/config/config.json` ← явное одобрение пользователя (HITL), см. Implementation Contract Ф7.
- **Blockers / open unknowns**: live-тесты зацикливания требуют `ROUTERAI_API_KEY` (env) — ключ из VS Code `${input:chat.lm.secret.-4792b31e}` скриптами не читается; без ключа Ф6 выполняется только как offline-каркас. Сохранённых логов прежнего зацикливания в `ai-core/logs` не найдено — регрессия строится на новых воспроизведениях.

---

**TL;DR**: Сделать конвейер `routerai-models` (fetch → filter → report → recommend → sync → compare) безопасным для регулярного запуска и корректным по данным: (1) заморозить базовую линию и убрать тихие перезаписи каталога; (2) ужесточить нормализацию/единицы тарификации и защиту от пустого/битого исходника; (3) исправить тиры и скоринг; (4) сделать синхронизацию с `ai-core` и VS Code «липкой» — сохранять рабочие ссылки и ключи, не менять кандидатов без обоснования; (5) добавить регрессионные тесты и systemd-семантику; (6) ввести **гейт надёжности агентного цикла** (50–100 итераций: повторы вызовов, бесконечный reasoning, повтор уже сделанного действия, завершение, восстановление после ошибки) с разделением «модель доступна» и «модели можно доверять управление агентом»; (7) только после прохождения гейта — выбрать эталон и применить конфиг в `ai-core` контролируемо (diff + явное одобрение). `deepseek/deepseek-v4-flash-0731` снимается с роли эталона/дефолта до прохождения тестов; `deepseek/deepseek-v4.1-flash` — отдельный кандидат; эталон не меняется «по названию версии».

---

## Context & Current State

### Архитектура конвейера (факт, HEAD 21197c1)
Все пути — из `filter_config.json → paths` (относительно корня репо); расположение ai-core — через `ai_core_paths.py` (env `AI_CORE_ROOT` > `filter_config.json → ai_core.project_root` = `/home/dev/projects/ai-core`; `AI_CORE_CONFIG` > `ai_core.config_path` = `config/config.json`).

```
fetch_models.py  → data/routerai_models_raw.json + data/meta.json      (GET https://routerai.ru/api/v1/models, БЕЗ ключа, 30s timeout)
filter_models.py → data/filtered_models.json + data/asr_models.json    (валидация, тиры, стоимость вызова)
build_report.py  → data/catalog.json + data/config_models.json + data/asr_catalog.json
                   + reports/models_report.html/.md                    (Jinja2 шаблон templates/report_template.html)
recommend.py     → data/recommendations.json                           (8 категорий, скоринг, топ-N)
sync_configs.py  → imports/chatLanguageModels.json [всегда при --apply]
                   + /home/dev/projects/ai-core/config/config.json     [только --apply]
                   + /home/dev/.vscode-server/data/User/chatLanguageModels.json [только --apply-clm]
compare_with_config.py → консольный/JSON отчёт (read-only)
run_all.py       → fetch→filter→report→recommend→[sync→compare]; fail-fast на первом ненулевом rc
```

### Контракт ai-core (источник истины генератора, проверено по живому config.json)
- Запись модели в `llm.providers.routerai.models[]`: `{id, name (с суффиксом "[tier]"), url, toolCalling, vision, maxInputTokens, maxOutputTokens}`. Голый `id` без префикса; `_meta`/`tier` в записи НЕ пишутся (тир — только в суффиксе name).
- Ссылки наружу — только `routerai:<id>`: `llm.default_model`, `model_overrides.{role}`, `multi_model_planning.planner_models[]`, `synthesizer_model`, `llm_model` в `browser_automation`, `through_browser`, `superjob.browser_automation`, `scoring`. Текущий `default_model` = `routerai:deepseek/deepseek-v4-flash-0731`.
- Провайдер в config.json: `{enabled, name, vendor:"customendpoint", api_key:"${input:chat.lm.secret.-4792b31e}", api_type:"chat-completions", base_url, models[]}`. **`api_key_priority` в ai-core НЕ используется** (это VS Code-конвенция).
- `api_key` в фильтр-конфиге: `sync.clm_api_key = "${input:chat.lm.secret.-77660a4d}"` — **отличается** от живого config.json (`-4792b31e`). Тот же `-77660a4d` и в `imports/chatLanguageModels.json`. → Решение (Q3): при `--apply` sync_configs **сохраняет** существующий `api_key` целевого config.json; `clm_api_key` используется только при создании блока с нуля.
- ASR в config.json: `asr.{personal_model:"microsoft/mai-transcribe-2", bulk_model:"qwen/qwen3-asr-0.6b", models:[]}`. Дефолты продублированы в ai-core (`core/voice_transcription.py` L47-48, `core/config_manager.py` L982). Два сценария: PERSONAL (качество, личный чат) и BULK (массово, дёшево) — **не заменять по общему рейтингу чат-моделей**. Алиасы приоритетов — в `filter_config.json → asr.personal_aliases/bulk_aliases`.
- VS Code источник — `/home/dev/.vscode-server/data/User/chatLanguageModels.json` (формат VS Code: провайдер с `apiKey` на уровне провайдера, поля модели `id/name/url/toolCalling/vision/maxInputTokens/maxOutputTokens`).

### Данные каталога (срез 2026-10-11)
- raw: 559 моделей; поля записи: `id, name, created(unix), description, context_length(0|null допустимо), architecture{modality, input_modalities[], output_modalities[], ...}, pricing{prompt, completion, input_cache_read?, seconds?, image_output?}, pricing_units{prompt:"token",...}, per_request_limits(null), supported_parameters[], default_parameters{}` + опциональные image-поля.
- **`deepseek/deepseek-v4-flash-0423` как ID отсутствует**. Есть `deepseek/deepseek-v4-flash` (display name "DeepSeek: DeepSeek V4 Flash 0423", created 2026-04-23), `deepseek/deepseek-v4-flash-0731` (created 2026-07-31) и `deepseek/deepseek-v4.1-flash` (created 2026-09-10). Все три: `context_length=1048576`, `supported_parameters` включают `tools`, `structured_outputs`, `reasoning`; `-0731` дешевле (`prompt 7.28e-06 ₽/токен` vs `1.05e-05` у v4-flash и `1.09e-05` у v4.1). → Решение (Q2, уточнено пользователем): 0731 **не** эталон до прохождения гейта надёжности; v4.1 — отдельный кандидат; эталон не меняется по одному названию.
- filtered: 298 (cheap 101 / medium 38 / premium 159); ASR: 22 (3 без цены); эталон в данных (`filtered_models.json`/`recommendations.json`) сейчас — 0731, потому что `filter_models.py` ищет `reference_model.id` из конфига (0423 → не найден → `reference_model: null`... расхождение: в `recommendations.json` эталон проставлен как 0731 — фактически recommend падает в фолбэк; см. issue 2).

### Подтверждённые баги и расхождения (карта для фаз)
1. **[P0/P2] Пустой/битый raw молча перезаписывает `filtered_models.json`** — `filter_models.py::filter_and_categorize` (≈L210) не имеет защиты «каталог слишком мал»; защита есть только в `sync_configs`/`ai_core_paths.py::check_catalog_ok` (L150). `build_report.py` при битом `recommendations.json` молча подставляет `{}` (JSONDecodeError→`{}`).
2. **[P1/P3] `reference_model.id = "deepseek/deepseek-v4-flash-0423"` в `filter_config.json` не существует в каталоге** → резолв эталона деградирует (reference=null / фолбэк в sync на «самую дешёвую cheap с tools»), штраф `reference_bonus`/замены моделей теряют якорь. Дефолты в `sync_configs.py` (L585, L681) — 0731 (рассинхрон с конфигом).
3. **[P1] Лимиты токенов разъехались**: фактически применяются `filter_config.json → token_limits` — вход cheap/medium/premium **100K/80K/64K**, выход 16K/8K/8K (видно в clm и config.json ai-core). README (L157-158, L275) и `ai_core_paths.py::DEFAULT_TOKEN_LIMITS` заявляют 250K/150K/100K. → Решение (Q1): **оставить фактическое 100K/80K/64K**, поправить README и дефолты кода. `apply_token_limits` (ai_core_paths.py ≈L287-330): `max_input=min(int(ctx)*0.8, потолок тира)`, `max_output=min(max(2048, ctx*0.2), потолок)`.
4. **[P3] `--dry-run` в `sync_configs.py` — фактически no-op** (dry-run и так по умолчанию); реальные флаги: `--apply`, `--apply-clm`, `--no-config`, `--catalog`. Семантика «dry-run ≠ не пишет» задокументирована неверно.
5. **[P3] Синхронизация полностью перезаписывает `models[]` топ-15** — существующие рабочие ссылки удаляются, если модель не попала в рейтинг; замена исчезнувшей ссылки (`find_replacement` L150) может увести роль на «любую самую дешёвую cheap с tools» без учёта назначения роли и гейта надёжности. `MODEL_REF_KEYS` (L115-124) объявлен, но в обходе не используется (обход ищет любые `routerai:`).
6. **[P3] `api_key` рассинхронизирован** (config.json `-4792b31e` vs filter_config/clm `-77660a4d`) — при текущем `--apply` ключ в config.json переписывается из `clm_api_key` (риск подмены рабочего ключа VS Code).
7. **[P4] `compare_with_config.py` печатает блок «△ РАСХОЖДЕНИЯ ПАРАМЕТРОВ» дважды** (≈L250 и L260); цены всегда помечаются расхождением (информационно).
8. **[P2/P4] Скоринг**: `novelty_bonus` (0–8) весь-90дней/спад-до-730дней — «новизна» 90 дней перевешивает пригодность (база 50, cost_bonus до 30); `obsolete_patterns` не покрывает `v3.1-`-поколения, но при этом паттерн `llama-3.1`/`qwen-2.5` штрафует актуальные семейства; `coding_bonus` = +12 только при `"coder" in id` (непрозрачно); `prefer_reasoning` (reviewer) = −6 без reasoning; качество считает `ctx/ref_ctx×0.35 + tools 0.2 + structured 0.15 + reasoning 0.2 + (ctx≥512K) 0.1`.
9. **[P4] Тиражи**: `assign_tier` (≈L160): cheap = prompt<100 И completion<100; premium = reasoning И (prompt≥100 ИЛИ completion≥100); medium = остальное. Границы непрозрачны и не вынесены целиком в конфиг (порог 100 дублируется, `require_reasoning` — в конфиге есть).
10. **[P1] `meta.json` пишется неатомарно** (fetch_models.py L76-105; raw — атомарно через `.tmp`+`replace`).
11. **[P4] `systemd/routerai-models-fetch.service`**: 5 отдельных строк `ExecStart` (fetch→filter→build_report→recommend→`sync_configs.py --apply`); при ненулевом rc одной строки **следующие команды всё равно выполняются** (в отличие от run_all.py); работает на системном `/usr/bin/python3` (не venv); env не передаётся; `TimeoutStartSec=180`; нет `OnFailure`/уведомлений. Таймер `*-*-1/2 05:30:00` (каждые 2 дня), `Persistent=true`, `RandomizedDelaySec=15min`. **`sync_configs.py --apply` в сервисе меняет ai-core config.json автоматически раз в 2 дня** — риск «случайной замены рабочих моделей», который план должен закрыть гейтами (Phases 4, 6, 7).
12. **[P2] ASR-конвейер не виден в верхнеуровневой схеме README**; цепочка: `filter_models.py::filter_asr_models` (audio→transcription; `rub_per_min = seconds×60`; cheap при `≤0.5 ₽/мин`) → `data/asr_models.json` → `build_report.py` → `data/asr_catalog.json` → `sync_configs.py::update_ai_core_asr` (≈L470, свой порог свежести `asr.stale_after_hours=72`).
13. **[P1] Тестов нет вообще** (ни tests/, ни pytest.ini); проверка — ручная таблица в README. Python 3.12 на хосте; зависимости `httpx>=0.27.0`, `jinja2>=3.1.0` установлены на системном python.
14. **[P1] `.env.example`/README**: переменная называется `AI_CORE_CONFIG_PATH`, код читает `AI_CORE_CONFIG` (ai_core_paths.py L87); `.env` не загружается (нет python-dotenv); `imports/copy` — файл-артефакт со старым форматом (не упомянут в README).
15. **[P0] Внешние вне-репо файлы, которые конвейер трогает**: `/home/dev/projects/ai-core/config/config.json` (--apply), `/home/dev/.vscode-server/data/User/chatLanguageModels.json` (--apply-clm). Бэкапы НЕ создаются («ai-core покрыт git»).

### Инварианты, которые нельзя нарушать
- Запись в `ai-core/config/config.json` и VS Code clm — только через явные флаги; `--dry-run`/`run_all.py` без `--apply` ничего не пишут.
- `asr.personal_model`/`bulk_model` не меняются по чат-рейтингу; personal и bulk — независимые сценарии (алиасы в `filter_config.json → asr.*`).
- Формат записи в ai-core: без `_meta`/`tier`/`api_key_priority`; ссылки — только `routerai:<id>` с голым id.
- `deepseek/deepseek-v4-flash-0731` не выбирается эталоном/дефолтом до прохождения гейта надёжности (Решение Q2).
- Лимиты токенов: фактическая политика 100K/80K/64K вход, 16K/8K/8K выход (Решение Q1).
- `api_key` целевого config.json сохраняется при записи (Решение Q3).
- Git: репо routerai-models — `21197c1`, ветка `main`. По одному коммиту на фазу. ai-core НЕ модифицируется в Фазах 1–6 (только чтение; запись config.json — в Фазе 7, после одобрения).

---

## Steps

Порядок работ: Ф1 (аудит) → Ф2 (источник данных) → Ф3 (тиры/рекомендации) → Ф4 (синхронизация) → Ф5 (тесты+инфраструктура) → Ф6 (гейт надёжности, live) → Ф7 (эталон, контролируемое применение, документация, приёмка).

### Фаза 1 — P0: аудит и заморозка базовой линии
*Dependencies: none.*
1. Создать `data/baseline/`; записать `baseline-<date>.json`: sha256 файлов `ai-core/config/config.json`, `/home/dev/.vscode-server/data/User/chatLanguageModels.json`, `imports/chatLanguageModels.json`; счётчики `data/filtered_models.json`/`catalog.json`/`recommendations.json`/`asr_catalog.json`; результат резолва эталона (см. Ф2/Ф3); вывод `python3 compare_with_config.py --json`.
2. Прогнать `python3 run_all.py` (без `--apply`) — зафиксировать коды/отчёты; сверить результат с базовой линией (должно быть 0 изменений в целевых конфигах).
3. Убедиться sha256 «до/после» целевых файлов идентичны; задокументировать список подтверждённых проблем (карта багов в Context) в `data/baseline/issues.md`.
4. Commit: «P0: baseline snapshot + issues map». Verification: `sha256sum` до==после; `git status --short` чист после коммита.

### Фаза 2 — P1: источники данных, нормализация, защита от пустых/битых данных
*Dependencies: Фаза 1.*
1. `filter_models.py`: жёсткий гейт на вход — если raw отсутствует/битый JSON/`data` пуст/валидных моделей `== 0` или `< min_valid_models` (новый ключ `validation.min_valid_models`, напр. 50) → stderr + exit 2, **выходные файлы НЕ перезаписываются** (атомарность сохранить через `.tmp`+`replace`; при провале каталог остаётся прежним). Аналогичный гейт минимального числа ASR. Добавить пункт в report-template/meta о причине отказа.
2. Нормализация: числовые значения строго через `pricing` + `pricing_units`; модели с `pricing.units != token` в чат-ветке помечать `pricing_unit_warning` (не отбрасывать, но и не смешивать с ₽/токен); `seconds`/`image_output` — только ASR/image-ветки. Недостающие поля — `null`/явные маркеры, а не 0 (сейчас `pricing.get("prompt", 0.0)` маскирует отсутствие как «бесплатно»).
3. `filter_models.py`/`recommend.py`: резолв эталона вынести в новый модуль `reliability.py` (см. Ф3); `reference_model` в выходах заполнять через `effective_reference_id()`, с полем `reference_resolution: {requested, effective, status: ok|missing|not_approved, reason}`.
4. `fetch_models.py`: атомарная запись `meta.json` (`.tmp`+`replace`); в meta добавить `api_base_url`, `git_revision` (git hash raw-снимка), `schema_version`; опциональную авторизацию через env `ROUTERAI_API_KEY` (будущее для live-тестов; сейчас — только если задан).
5. `build_report.py`: при битом `recommendations.json` — не тихий `{}`, а warning в отчёт + exit 1 (hard fail на поломанном входе).
6. Тесты: `tests/test_source.py` (см. Ф5-инфраструктуру): пустой raw не перезаписывает filtered; битый JSON → exit 2; meta атомарна; единицы тарификации не смешиваются; `min_valid_models` срабатывает.
7. README/.env.example: поправить `AI_CORE_CONFIG` (не `AI_CORE_CONFIG_PATH`); актуализировать числа (559/298/101/38/159).
Verification: `python3 -m pytest tests/test_source.py -q` зелёный; подмена raw на `{"data":[]}` → `filter_models.py` exit 2 и старый filtered нетронут; `python3 run_all.py` exit 0 (после восстановления raw).

### Фаза 3 — P2: тиры, скоринг, рекомендации, гейт эталона
*Dependencies: Фаза 2.*
1. Новый модуль `reliability.py`: `effective_reference_id(cfg, catalog)`, `is_model_approved(id, cfg)`, `reference_eligibility_notes(...)`. Конфиг-блок в `filter_config.json`:
   ```json
   "reference_model": {
     "id": null,
     "approved_ids": [],
     "watchlist": ["deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash"],
     "fallback": "cheapest_cheap_tools_not_watchlisted",
     "label": "Эталон: временный авто-выбор до прохождения гейта надёжности"
   }
   ```
   Логика: если `id` задан и в `approved_ids` → он эталон; иначе авто-выбор: самая дешёвая (по `cost_per_turn_estimate_rub`) cheap-модель с tools НЕ из `watchlist`; `status` в выходах фиксирует причину. `recommend.py::score_model` применяет `reference_bonus` только к approved-эталону.
2. `filter_models.py::assign_tier`: порог 100 и `require_reasoning` читать из конфига (`tiers.*`); добавить валидацию непересечения/полноты по факту (уже частично есть: RuntimeError на дублях, assert на потери) + выводить `tier_boundaries` в filtered.
3. `recommend.py`: новизна — снизить вес: `novelty_bonus` только для моделей ≤ `novelty_window_days` И `created` известен; добавить `novelty_ceiling_ratio` (напр. не более 20% от base_score) — чтобы новизна не перевешивала пригодность. `obsolete_patterns`: убрать паттерны, штрафующие актуальные семейства (проверить каждый против каталога: не штрафовать `llama-3.1` актуальные снапшоты — сузить до точных имён снапшотов); добавить поколения `v3.1`/`v3.2` из глубоко устаревших — ТОЛЬКО если модель не в `approved_ids`. `coding_bonus`: заменить эвристику `"coder" in id` на `coding` категорийный критерий (id/name содержит coder/code/agent и т.п.) + задокументировать.
4. Отбор в `sync_configs.py::select_top_models`: гибридный score — оставить; добавить `DefaultAgentGate`: ни один кандидат категории `default_agent`/эталон не может выйти в топ и попасть на роль default, если он в `watchlist` и не в `approved_ids` (пока не пройден гейт) — такие модели помечаются в отчёте «ожидает подтверждения надёжности», а на их место добор.
5. Тесты: `tests/test_filtering.py`: границы тиров (99.9/100.0), непересечение, дефицит категорий (всего 2 модели → отбор срабатывает без фиктивных записей), новизна не перевешивает базу, watchlist-модель не становится default/reference, фолбэк эталона.
Verification: pytest зелёный; `recommend.py` при конфиге с `reference_model.id=null` выбирает авто-эталон не из watchlist; в `recommendations.json` поле `reference_resolution.status` корректно.

### Фаза 4 — P3: безопасная синхронизация с ai-core и VS Code
*Dependencies: Фаза 3.*
1. `sync_configs.py` — семантика флагов: `--dry-run` — реальный no-write c diff (рефакторинг: по умолчанию dry-run; `--apply` — запись config.json ai-core + imports/clm; `--apply-clm` — + VS Code clm; `--no-config`, `--catalog` — как есть). После любого запуска печатать «записанные файлы: [...] | не тронуты: [...]».
2. **«Липкая» политика обновления набора моделей** (новый ключ `sync.sticky_existing: true`): при `--apply` набор `models[]` = объединение (а) моделей из текущего config.json, которые ВСЁ ЕЩЁ есть в каталоге (сохранить существующие рабочие, НЕ удалять только из-за рейтинга), и (б) топ-15 отбора (квоты 10+2+3, эталон первым — но под гейтом Ф3). Удаление модели из набора — только если её нет в каталоге И она нигде не запрашивается ссылкой; иначе — proposal в diff с требованием подтверждения.
3. Ссылки `routerai:<id>`: `find_model_refs` — задействовать `MODEL_REF_KEYS` (или удалить неиспользуемый набор); замена исчезнувшей модели (`find_replacement`) — приоритет уже есть (тот же провайдер cheap+tools → approved-эталон → дешёвая cheap+tools), но: (а) никогда не выбирать модель из `watchlist` без одобрения; (б) учитывать назначение роли (для `reviewer_model` — reasoning; для long-context — контекст и т.п. — постройка на категориях рекомендаций); (в) если подходящей нет — НЕ переназначать молча: пометить в diff `REPLACEMENT_NEEDED` и выйти с кодом 2 в режиме `--strict-refs` (новый флаг; в обычном режиме — warning).
4. `api_key`: при записи сохранять текущее значение из целевого config.json (Решение Q3); `clm_api_key` только при создании провайдера с нуля.
5. `compare_with_config.py`: убрать дублирующийся печатный блок (≈L250/260); добавить предупреждение о stale-каталоге и колонку «новая/удалена».
6. Тесты: `tests/test_sync.py`: `--dry-run` не пишет файлы; sticky-набор сохраняет существующую модель, отсутствующую в топе; исчезнувшая ссылка → замена не из watchlist / `--strict-refs` exit 2 при отсутствии кандидата; `api_key` сохранён; `--apply` не трогает VS Code clm без `--apply-clm`; атомарность (tmp+replace) и код 2 при stale-каталоге.
Verification: pytest зелёный; `python3 sync_configs.py --dry-run` не изменяет ни один целевой файл (sha256 до==после); `python3 run_all.py --apply` на test-копиях (через env `AI_CORE_CONFIG`/`ROUTERAI_CATALOG` на временные файлы) даёт ожидаемый diff.

### Фаза 5 — P4a: регрессионные тесты, конвейер, systemd
*Dependencies: Фаза 4.*
1. Инфраструктура: `pytest.ini` (markers: live), `requirements-dev.txt` (pytest), `tests/conftest.py` — helpers: загрузка конфига/fixtures из временных корней; env-переменные боевых путей (`AI_CORE_CONFIG`, `ROUTERAI_CATALOG`, `ROUTERAI_ASR_CATALOG`) — тесты никогда не пишут в реальные файлы.
2. Покрыть таблицу сценариев отказа (из запроса): API пуст → exit 2 + конфиг цел; битый JSON → диагностика; изменился формат поля → предупреждение (schema guard с расширяемым списком ожидаемых ключей); изменились цены → пересчёт тира/стоимости/рекомендаций (инвариант: сумма цен в выводе согласована с входом); исчезла модель → устаревшая ссылка + безопасная замена; дубликаты ID → ошибка до формирования конфига (проверка в filter_models: уникальность `id`); каталог старше `stale_after_hours` → запись запрещена (уже есть `check_catalog_ok`); нехватка квоты → корректный дефицит; `--dry-run` → 0 изменений; `--apply` → только предусмотренные файлы; `--apply-clm` → только при флаге; ошибка промежуточного этапа → последующие не запускаются (run_all semantics + тест на systemd-модель).
3. `systemd/routerai-models-fetch.service`: заменить 5 ExecStart-строк на один `ExecStart=/usr/bin/python3 run_all.py --apply` (fail-fast как у run_all; с `--no-fetch` в отдельном прогоне не надо — таймер уже задаёт периодичность); `Environment=`/`EnvironmentFile=` для опционального ключа; `OnFailure` (уведомление в журнал); `StandardOutput/Error=journal` сохранить; `TimeoutStartSec=180` сохранить. Проверить права записи WorkingDirectory от `User=dev` (data/, imports/, reports/ — принадлежат dev).
4. README: актуализировать схему конвейера (включая ASR-цепочку и `run_all.py`), таблицу флагов, раздел «Проверка» с pytest-командами; отметить `imports/copy` как устаревший (рекомендация удалить отдельным коммитом — вычистить в этой фазе, файл в git history сохранится).
5. Проверка systemd: `system-analyze verify`/`systemctl --user` не применимо (системный unit) — проверка синтаксиса `systemd-analyze verify` по возможности; в CI/локально — прогон `run_all.py` с test-путями.
Verification: `python3 -m pytest -m "not live" -q` — весь офлайн-набор зелёный; `systemd-analyze verify systemd/routerai-models-fetch.service` без ошибок (или задокументированный отказ окружения).

### Фаза 6 — P4b: гейт надёжности агентного цикла (live)
*Dependencies: Фаза 5; требует `ROUTERAI_API_KEY` в env (VS Code input-переменную скрипты не читают — задокументировать получение ключа).*
1. Сначала диагностика: вычитать `ai-core/logs/*` на предмет сохранённых следов зацикливания (grep: повторяющиеся `finish_reason`, одинаковые `tool_calls`, `iteration`-счётчики). По факту exploration логов с явными следами НЕ найдено — задокументировать, что регрессия строится на новых воспроизведениях; если найдёте — преобразовать tail-сообщений в сценарии.
2. `tests/test_loop_reliability.py` (marker `live`, отдельно от CI): драйвер поверх httpx → POST `https://routerai.ru/api/v1/chat/completions` (Bearer `ROUTERAI_API_KEY`); сценарии по 50–100 итераций с реальными переходами reasoning→tool_call→наблюдение:
   - повторные идентичные вызовы инструмента (детектор: один и тот же `tool_call.id/имя+аргументы` N раз подряд);
   - повторение одного текста без вызова инструмента;
   - бесконечное продолжение reasoning (не завершается за лимит итераций);
   - повтор уже завершённого действия (движение состояния вперёд);
   - корректное завершение задачи (терминальный ответ с `finish_reason=stop`);
   - восстановление после ошибки инструмента (переход к другому действию);
   - рост числа повторов при росте истории (прогон на 2k токенов и на 60k токенов).
   Каждый прогон сохраняет JSON-артефакт `data/reliability/runs/<model>-<date>.json` (все обертки, finish_reason, index итераций, verdict: pass/fail + счетчики).
3. Кандидаты (отдельный прогон на каждого): `deepseek/deepseek-v4-flash-0731`, `deepseek/deepseek-v4.1-flash`; плюс 1-2 топовых из категории `default_agent` по текущему каталогу (например `qwen/qwen3.7-flash`) — как альтернативы. V4.1 НЕ считается автоматически надёжным «из-за нового поколения» — тот же прогон.
4. Правило принятия: модель проходит, если все сценарии на обоих размерах истории дают verdict pass с порогом повторов = 0 (или заданным в конфиге `reliability.max_repeats`, по умолчанию 0) и корректным завершением. Число итераций само по себе не критерий — важен реальный переход между действиями.
5. Вариант вывода В отчёт `data/reliability/summary.json`: таблица кандидат → verdict → счётчики → рекомендация; HTML-приложение к `reports/`.
Verification: `ROUTERAI_API_KEY=... python3 -m pytest tests/test_loop_reliability.py -m live -q` — зелёный набор verdict'ов; `data/reliability/summary.json` сформирован; без ключа — тесты пропускаются с явным сообщением.

### Фаза 7 — эталон, контролируемое применение, документация, приёмка
*Dependencies: Фаза 6; запись в ai-core — по явному одобрению пользователя.*
1. Решение по эталону (на основе summary.json): прошедшие тесты модели попадают в `reference_model.approved_ids`; `reference_model.id` устанавливается на лучшего прошедшего кандидата (приоритет: v4.1-flash если прошёл и стабилен; иначе другой approved; 0731 — только если прошёл гейт). Не прошедшие — остаются в `watchlist`, исключаются из роли default (через DefaultAgentGate), в конфиг ai-core не назначаются.
2. Контролируемое применение: `python3 run_all.py --apply` с env на боевые пути → проверить diff `git -C /home/dev/projects/ai-core diff config/config.json`; **запись в конфиг ai-core выполняется ТОЛЬКО после демонстрации diff и явного подтверждения пользователем** (stop condition фазы); `--apply-clm` — только по отдельному запросу. Подтвердить: `default_model` указывает на approved; ссылки ролей не указывают на watchlist; набор моделей липкий (рабочие сохранены); `api_key` сохранён.
3. README: согласовать всё с фактическим поведением: лимиты токенов 100K/80K/64K (Решение Q1), эталон/гейт надёжности (решение по Ф6), ASR-цепочка, флаги, тесты, systemd.
4. Приёмка: `python3 -m pytest -m "not live" -q` зелёный; `python3 run_all.py` exit 0 (свежие отчёты в reports/); `run_all.py --apply` на воспризводимом diff; финальный `data/baseline/acceptance-<date>.md`: изменения, результаты тестов (включая live-гейт), оставшиеся риски.
Verification: все пункты выше; sha256-сверка clm через `--apply-clm` только при флаге; финальный отчёт существует и содержит список изменений/тесты/риски.

---

## Relevant Files & Symbols

`/home/dev/projects/routerai-models/` (репо) — если не указано иное:
- `filter_config.json` — Modify. Ключи: `validation.min_valid_models`, `reference_model` (id/approved_ids/watchlist/fallback), `sync.sticky_existing`, `sync.strict_refs`, `reliability.max_repeats`; `token_limits` и `paths` остаются. Порог `tiers.*` вынести в явные границы.
- `reliability.py` — **Create**. Функции: `effective_reference_id(cfg, catalog)`, `is_model_approved(id, cfg)`, `model_on_watchlist(id, cfg)`, `reference_eligibility_notes(cfg, catalog)`; константы ролей для gate.
- `fetch_models.py` — Modify. Атомарный `meta.json`, доп. meta-поля, опциональный `ROUTERAI_API_KEY`.
- `filter_models.py` — Modify. Гейт min-valid (не перезаписывать выходы при пустом/битом), нормализация с `pricing_units` (маркеры вместо 0), резолв эталона через `reliability.py`, `tier` из конфига, уникальность `id`, `filter_asr_models` (L283) гейт минимума ASR.
- `recommend.py` — Modify. `CATEGORY_DEFS` (L30-90), `score_model` (≈L150): новый `novelty_bonus` (ceiling), уточнённые `obsolete_patterns`, `reference_bonus` только для approved, `coding_bonus` без эвристики `"coder" in id`.
- `sync_configs.py` — Modify. `--dry-run` реальный no-op (L711+ семантика флагов), sticky-набор (L546-571), `find_replacement` (L150) с учетом роли/watchlist, `--strict-refs`, сохранение api_key, `MODEL_REF_KEYS` (L115-124) в обходе, `select_top_models` (≈L230) с DefaultAgentGate, `update_ai_core_asr` (≈L470) — без изменений по моделям personal/bulk (только каталог `models`).
- `compare_with_config.py` — Modify. Дубль блока печати (≈L250/260), stale-warning.
- `ai_core_paths.py` — Modify. `DEFAULT_TOKEN_LIMITS` → 100K/80K/64K вход, 16K/8K/8K выход (Решение Q1); `DEFAULT_CONTEXT_LENGTH` остаётся 128_000.
- `build_report.py` — Modify. Битый recommendations → hard fail + warning в отчёт; meta-блок (source/timestamp/schema) в отчёт и `catalog.json`.
- `run_all.py` — Modify (минимально). Сохранить fail-fast; добавить передачу env; (опц.) `--report-only`.
- `templates/report_template.html` — Modify. Блок «», flag watchlist/approved, meta/источник/время получения.
- `pytest.ini`, `requirements-dev.txt`, `tests/conftest.py`, `tests/test_source.py`, `tests/test_filtering.py`, `tests/test_sync.py`, `tests/test_loop_reliability.py` — **Create**.
- `systemd/routerai-models-fetch.service` — Modify. Один ExecStart `run_all.py --apply`, env-file, OnFailure. `systemd/routerai-models-fetch.timer` — Reuse.
- `data/baseline/`, `data/reliability/` — **Create** (артефакты фаз 1, 6).
- `README.md`, `.env.example` — Modify (лимиты, AI_CORE_CONFIG, числа, ASR-цепочка, флаги, pytest).
- `imports/copy` — Remove (артефакт старого формата; в истории git сохранится).
- Вне репо (только чтение в Ф1-Ф6): `/home/dev/projects/ai-core/config/config.json`; запись — Ф7 по одобрению. `/home/dev/.vscode-server/data/User/chatLanguageModels.json` — только `--apply-clm`.

## Verification

1. Пофазно — см. «Verification» каждой фазы выше (точные команды и ожидаемый результат).
2. Топ-уровень: `cd /home/dev/projects/routerai-models && python3 -m pytest -m "not live" -q` — все офлайн-тесты зелёные.
3. `python3 run_all.py` — exit 0, свежие `reports/models_report.{md,html}`; `python3 compare_with_config.py --json` — без несоответствий.
4. `sha256sum` целевых файлов до/после dry-run — идентичны; после `--apply` (Ф7) — diff содержит только предусмотренные изменения (липкий набор, без watchlist-дефолта, api_key сохранён).
5. Финальный отчёт `data/baseline/acceptance-<date>.md` содержит изменения, результаты тестов (вкл. live-гейт) и оставшиеся риски.

## Risks & Rollback

- **Подмена рабочего ключа/config ai-core**: закрыто Решением Q3 (сохранение api_key) и sticky-политикой; откат — `git -C /home/dev/projects/ai-core checkout config/config.json` (но без `--reset --hard`; при необходимости — ручной возврат).
- **0713/V4.1 зацикливание**: гейт Ф6 даёт verdict до любых назначений; watchlist не попадает в default до approved. Риск: «ложно-прямой» проход при недостаточном числе итераций — компенсируется требованием реальных переходов действия и прогоном на длинной истории.
- **Изменение формата API RouterAI** (переименование полей каталога): schema guard в Ф2 детектирует; конвейер падает явно, конфиги не трогаются.
- **Пустой каталог ночью по таймеру**: гейт min-valid в Ф2 и check_catalog_ok в sync не дадут записать пустоту; откат — предыдущий raw/catalog (снапшоты в data/ + git).
- **Тесты live дорогие/требуют ключ**: без ключа — skip с сообщением; Ф6 не блокирует Ф1-Ф5 (офлайн).
- **Регрессия порогов токенов**: после выравнивания дефолтов (100K/80K/64K) у «премиальных» моделей вход не превышает 64K — при необходимости лимит правится в одном месте `token_limits`.
- Деструктивные git-операции не используются; каждую фазу — отдельный коммит.

## Decisions & Scope Boundaries

- **Included**: routerai-models код+конфиг+тесты+systemd+README; гейт надёжности (live-тесты); контролируемое обновление `ai-core/config/config.json` и VS Code clm (только через флаги и одобрение); выравнивание документации по факту.
- **Excluded**: переписывание архитектуры конвейера; авто-замена рабочих моделей без diff/одобрения; изменение `asr.personal_model`/`bulk_model` по чат-рейтингу; правки исходников ai-core (только конфиг-файлы через sync); интеграция с GitHub Actions/CI-серверами; изменение endpoint-структуры RouterAI API.
- **Key Decisions**: см. Decision Record.

## Decision Record
- Q1-Лимиты токенов: Problem: README/дефолты кода (250K/150K/100K) против фактического filter_config (100K/80K/64K) — какой политике верить? Evidence: в clm и config.json ai-core применяются 100K/80K/64K. Options: оставить факт + выровнять доки (min diff, поведение не меняется); перейти на 250K+; динамика от контекста. Selected: **оставить факт 100K/80K/64K**, поправить README и `DEFAULT_TOKEN_LIMITS`. Why: стабильный проверяемый результат, минимум изменений. Rejected: 250K — крупный diff и риск ложной уверенности в лимитах API; динамика — требует верификации лимитов каждой модели.
- Q2-Эталон: Problem: `reference_model.id=deepseek/deepseek-v4-flash-0423` не существует в каталоге; 0731 предложен, но известны публичные сообщения о зацикливаниях (04.08, 14.08, 20.08.2026); v4.1 вышла 10.09.2026, V4 объявлен снятым с эксплуатации у DeepSeek. Evidence: raw-каталог: есть `deepseek-v4-flash`(name "V4 Flash 0423"), `-0731`, `-v4.1-flash`; логов зацикливания в ai-core/logs не найдено. Options: закрепить 0731; оставить 0423 «разбираться»; гейт надёжности. Selected (по указанию пользователя): **0731 снимается с роли эталона до прохождения тестов; v4.1 — отдельный кандидат; approved/watchlist в `reference_model`; эталон ≡ approved**. Why: управляющей модели опасен цикл хуже цены/задержки; «актуальность ID» ≠ «надёжность управления». Rejected: закрепить 0731 — игнорирует публичные случаи циклов.
- Q3-api_key: Problem: config.json ai-core хранит `${input:chat.lm.secret.-4792b31e}`, filter_config/clm — `-77660a4d`. Options: всегда писать clm_api_key; сохранять целевой api_key. Selected: **сохранять существующий api_key целевого config.json**; clm_api_key — только при создании блока. Why: исключает случайную подмену рабочего ключа VS Code. Rejected: всегда писать — риск сломать рабочий ключ при рассинхроне хэшей.
- D4-sticky-синхронизация: Problem: текущий sync перезаписывает models[] топ-15, удаляя рабочие модели вне рейтинга и молча переназначая роли. Options: полная замена; липкий набор + proposal + strict-refs. Selected: **`sync.sticky_existing: true`**, удаление только при отсутствии в каталоге и без ссылок, `--strict-refs` → exit 2 при отсутствии кандидата. Why: сохраняет рабочие роли, не применяет замену без подходящей модели. Rejected: полная замена — риск «случайной замены рабочих моделей».
- D5-эталон авто-выбор до гейта: Problem: без `reference_model.id` рекомендации теряют якорь стоимости. Options: null → деградация; фолбэк «самая дешёвая cheap+tools не из watchlist». Selected: **авто-фолбэк с `reference_resolution.status`** и бейджем в отчёте. Why: прозрачный временный эталон до гейта.

## Implementation Contract (per phase)

### Фаза 1 (P0)
- **Base revision**: `21197c1` (routerai-models). **Allowed files**: `data/baseline/**` (новые артефакты). **Required changes**: снапшот+issues-map; права на чтение `ai-core/config/config.json` и VS Code clm (без записи). **Preserve**: все существующие файлы репо — без изменений. **Out of scope**: любые правки кода/конфигов/README. **Verification**: sha256 целевых файлов до==после `run_all.py`; `git status --short` чистый после коммита. **Stop conditions**: невозможность запустить `python3 run_all.py` без сети → зафиксировать и остановить.

### Фаза 2 (P1)
- **Base revision**: коммит Ф1. **Allowed files**: `filter_config.json`, `fetch_models.py`, `filter_models.py`, `build_report.py`, `README.md`, `.env.example`, `tests/test_source.py`, `tests/conftest.py`, `pytest.ini`, `requirements-dev.txt`. **Required changes**: гейт min-valid (+ атомарность), нормализация единиц тарификации, атомарный meta, hard-fail на битом recommendations, env-имя. **Preserve**: формат выходных файлов (поля filtered/catalog/config_models/recommendations) — совместимость с Ф3-Ф7; `token_limits` (пока факт 100K/80K/64K). **Out of scope**: тиры/скоринг/синхронизация (Ф3/Ф4), systemd (Ф5). **Verification**: `python3 -m pytest tests/test_source.py -q`; подмена raw на пустой → exit 2, filtered не тронут. **Stop conditions**: обнаружено изменение формата API → задокументировать и остановить (не подгонять схему молча).

### Фаза 3 (P2)
- **Base revision**: коммит Ф2. **Allowed files**: `reliability.py` (новый), `filter_config.json`, `filter_models.py`, `recommend.py`, `sync_configs.py` (only `select_top_models` gate часть), `tests/test_filtering.py`. **Required changes**: гейт эталона/approved/watchlist; тиры из конфига; новизна/obsolete/coding фиксы; DefaultAgentGate. **Preserve**: квоты 10+2+3 и весá категорий; гибридный score; формат recommendations.json; эталон в данных никогда не из watchlist. **Out of scope**: write-семантика sync (Ф4), тесты live (Ф6). **Verification**: pytest зелёный; `recommend.py` с `id:null` даёт авто-эталон не из watchlist; `reference_resolution.status` заполнен. **Stop conditions**: approved_ids пуст и фолбэк невозможен (нет cheap+tools вне watchlist) → решить с пользователем.

### Фаза 4 (P3)
- **Base revision**: коммит Ф3. **Allowed files**: `sync_configs.py`, `compare_with_config.py`, `filter_config.json`, `tests/test_sync.py`. **Required changes**: флаги/семантика dry-run→apply; sticky-набор; find_replacement с учётом роли/watchlist; `--strict-refs`; сохранение api_key; dedupe в compare. **Preserve**: ai-core контракт (формат записей, `routerai:<id>`, без `_meta`); clm-схема; лимиты; ASR personal/bulk. **Out of scope**: реальная запись в боевой config.json/VS Code clm (Ф7, по одобрению). **Verification**: pytest; dry-run не пишет (sha256); `--apply` на временных env-путях даёт ожидаемый diff; без `--apply-clm` VS Code clm не тронут. **Stop conditions**: тест требует записи в боевой файл → не выполнять, использовать временные пути.

### Фаза 5 (P4a)
- **Base revision**: коммит Ф4. **Allowed files**: `tests/**` (кроме live-набора Ф6), `pytest.ini`, `requirements-dev.txt`, `systemd/routerai-models-fetch.service`, `run_all.py` (если требуется для env/fail-fast), `README.md`, `imports/copy` (remove). **Required changes**: полный регресс-набор (таблица сценариев отказа), systemd одним ExecStart + env/OnFailure, readme-актуализация. **Preserve**: флаги конвейера; формат unit-файла; `run_all.py` fail-fast (тест на этот инвариант). **Out of scope**: live-тесты (Ф6), боевой крон. **Verification**: `python3 -m pytest -m "not live" -q` зелёный; `systemd-analyze verify` без ошибок (или задокументировано). **Stop conditions**: изменение systemd unit, которое ломает синтаксис → вернуть и задокументировать.

### Фаза 6 (P4b, live)
- **Base revision**: коммит Ф5. **Allowed files**: `tests/test_loop_reliability.py`, `data/reliability/**`, `filter_config.json` (reliability.max_repeats), `templates/report_template.html` (вывод verdict). **Required changes**: live-драйвер + 7 сценариев на 50-100 итераций (2 размера истории), summary.json, правило прохождения. **Preserve**: не менять производственный код конвейера (только тест/конфиг); не трогать боевые конфиги. **Out of scope**: назначение эталона (Ф7). **Verification**: с ключом — запуск всех кандидатов, verdict'ы в `data/reliability/summary.json`; без ключа — skip с сообщением. **Stop conditions**: ключ недоступен → отметить риск, продолжить без live-verdict'ов, не назначать эталон.

### Фаза 7 (приёмка)
- **Base revision**: коммит Ф6. **Allowed files**: `filter_config.json` (approved_ids/id), `README.md`, `data/baseline/acceptance-<date>.md`; **только по одобрению пользователя**: запись через `python3 run_all.py --apply [--apply-clm]` в `ai-core/config/config.json` и путь VS Code. **Required changes**: фиксация эталона по verdict'ам; контролируемое применение (diff → одобрение → запись); финальные доки и отчёт. **Preserve**: инварианты всех фаз (лимиты, липкий набор, api_key, ASR, гейт). **Out of scope**: что-либо ещё в ai-core. **Verification**: pytest (not live) зелёный; diff в ai-core содержит только предусмотренные изменения; сommit по фазам; acceptance-отчёт. **Stop conditions**: любой несоответствующий инварианту diff → НЕ писать, показать diff пользователю.

## Further Considerations
1. **Публикация гейта надёжности**: live-тест Ф6 стоит прогонять периодически (например, ручным запуском после каждой смены каталога с таймером) — сейчас это разовый прогон; авто-периодизация — отдельное решение.
2. **`imports/copy` и прочие недокументированные артефакты** — вычищаются в Ф5; держать репозиторий «как есть» до этого, чтобы не смешивать аудит и изменения.
3. **Итог по решению пользователя зафиксирован**: эталон не закрепляется «по названию версии»; необходимость проверки обоих вопросов (актуальность/доступность через RouterAI и допуск к управлению агентом) заложена в архитектуру Ф2/Ф3/Ф6.
