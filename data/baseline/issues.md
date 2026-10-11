# Issues map — подтверждено при заморозке базовой линии (2026-10-11)

Базовая линия: commit `21197c1`, прогон `python3 run_all.py` (без `--apply`) 2026-10-11T15:22Z.
Целевые конфиги после прогона **не изменены** (sha256 до==после, см. `baseline-2026-10-11.json`).

## P0 — воспроизведено в контрольном прогоне

| # | Приоритет | Проблема | Доказательство | Фаза |
|---|-----------|----------|----------------|------|
| 2 | P1 | `filter_config.json → reference_model.id = "deepseek/deepseek-v4-flash-0423"` не существует в каталоге → `filter_models.py` пишет `reference_model: null`; `recommend.py` падает: `Ошибка: эталон deepseek/deepseek-v4-flash-0423 не найден в каталоге` | `run_all.py` rc=1 на шаге `recommend`; `filtered_models.json → reference_model: null`; каталог содержит `deepseek/deepseek-v4-flash` (name "V4 Flash 0423", created 2026-04-23), `-0731`, `-v4.1-flash` — ID без суффикса даты | Ф2/Ф3 |
| 1 | P0 | Пустой/битый raw молча перезаписывает фильтрованный каталог — защита min-valid отсутствует в `filter_models.py::filter_and_categorize` | Код (≈L210) без гейта; защита только в `sync_configs`/`ai_core_paths.check_catalog_ok` | Ф2 |
| 15 | P0 | Внешние файлы (`ai-core/config/config.json`, VS Code clm) без бэкапов; бэкапы не создаются | Инвариант проверен: sha256 до==после без `--apply` — запись только по флагам | Ф4 |
| 10 | P1 | `meta.json` пишется неатомарно | `fetch_models.py` L76-105 (в отличие от raw — атомарный `.tmp`+`replace`) | Ф2 |

## P1 — подтверждено кодом/данными

| # | Приоритет | Проблема | Доказательство | Фаза |
|---|-----------|----------|----------------|------|
| 13 | P1 | Тестов нет вообще (ни tests/, ни pytest.ini) | `ls tests/` — нет; проверка ручная | Ф5 |
| 14 | P1 | `.env.example`/README: `AI_CORE_CONFIG_PATH` vs код `AI_CORE_CONFIG`; `.env` не загружается; `imports/copy` — устаревший артефакт | `ai_core_paths.py` L87 читает `AI_CORE_CONFIG` | Ф2/Ф5 |
| 3 | P1 | Лимиты токенов разъехались: факт `filter_config.token_limits` 100K/80K/64K, 16K/8K/8K; README/`DEFAULT_TOKEN_LIMITS` — 250K/150K/100K | В clm и config.json ai-core применяются 100K/80K/64K | Ф2 (доки) |
| 6 | P3 | `api_key` рассинхронизирован: config.json `-4792b31e` vs filter_config/clm `-77660a4d`; `--apply` перепишет ключ из `clm_api_key` | Сравнение конфигов | Ф4 |

## P2/P3/P4 — подтверждено кодом и данными

| # | Приоритет | Проблема | Доказательство | Фаза |
|---|-----------|----------|----------------|------|
| 4 | P3 | `--dry-run` — фактически no-op (dry-run и так по умолчанию); реальные флаги `--apply`, `--apply-clm`, `--no-config`, `--catalog` | `sync_configs.py` L711+ | Ф4 |
| 5 | P3 | Синхронизация перезаписывает `models[]` топ-15; `find_replacement` может увести роль на «любую дешёвую cheap+tools» без учёта роли/гейта; `MODEL_REF_KEYS` объявлен, но не используется | `sync_configs.py` L115-124, L150, L546-571 | Ф4 |
| 7 | P4 | `compare_with_config.py` печатает блок «△ РАСХОЖДЕНИЯ ПАРАМЕТРОВ» дважды (≈L250 и L260); цены всегда в расхождениях | Код | Ф4 |
| 8 | P2 | Скоринг: `novelty_bonus` 90 дней перевешивает пригодность; `obsolete_patterns` штрафует актуальные семейства (`llama-3.1`, `qwen-2.5`); не покрывает `v3.1-`; `coding_bonus` = +12 только при `"coder" in id`; `prefer_reasoning` (reviewer) = −6 без reasoning | `recommend.py` ≈L150 | Ф3 |
| 9 | P4 | Тиражи: порог 100 и `require_reasoning` не вынесены в конфиг | `filter_models.py::assign_tier` ≈L160 | Ф3 |
| 12 | P2 | ASR-конвейер не виден в верхнеуровневой схеме README; отдельный порог свежести 72ч | `sync_configs.py::update_ai_core_asr` ≈L470 | Ф5 (README) |
| 11 | P4 | `systemd/routerai-models-fetch.service`: 5 ExecStart-строк без fail-fast; системный python; нет env/OnFailure; `--apply` раз в 2 дня меняет ai-core config.json | Unit-файл | Ф5 |

## Наблюдения прогона (помимо карты багов плана)

- **Дрейф тиров между прогонами**: первый прогон дал stats cheap 101/medium 38/premium 159, повторный fetch → cheap 102/medium 38/premium 158. Причина: свежий снапшот API 2026-10-11T15:22Z (ценовой дрейф или новая/изменившаяся модель). Значит тиры **не детерминированы** между прогонами — ожидаемо при живой цене, но важно для тестов (Ф3/Ф5): тесты не должны зависеть от абсолютных счётчиков.
- `recommendations.json` остался от прежнего прогона (reference 0731), т.к. `recommend` упал — **устаревшие артефакты не перезаписываются при падении шага** (fail-fast работает).
- `catalog.json`/`config_models.json` перегенерированы с новым raw — счётчики могут слегка отличаться от baseline при последующих прогонах (живой источник).

## Инварианты, подтверждённые в Ф1

- [x] sha256 `ai-core/config/config.json`, VS Code clm, `imports/chatLanguageModels.json` — до == после `run_all.py` (без `--apply`).
- [x] Работа сети: `fetch_models` получил 559 моделей (2026-10-11T15:22Z).
- [x] Пути конвейера соответствуют `filter_config.json → paths`; ai-core найден через `ai_core_paths.py`.