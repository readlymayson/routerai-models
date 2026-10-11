# routerai-models — парсер и аналитика каталога моделей routerai.ru

Регулярная выгрузка каталога `https://routerai.ru/api/v1/models`, валидация
чат-моделей (на входе text, на выходе text + поддержка `tools`, включая
vision-модели с изображениями на входе), непересекающийся тиринг по стоимости
вызова (в ₽ за 1M токенов), сравнение с конфигурацией `ai-core`, рекомендации по
задачам (fit-to-purpose) и генерация интерактивного HTML-отчёта.

## Архитектура

```
routerai-models/
├── fetch_models.py           # выгрузка каталога с API → data/routerai_models_raw.json
├── filter_models.py          # валидация + тиринг → data/filtered_models.json
├── build_report.py           # каталог + конфиг + HTML/MD-отчёт (+ рекомендации)
├── recommend.py              # скоринг fit-to-purpose + категории → data/recommendations.json
├── compare_with_config.py    # diff с ai-core/config/config.json (+ clm, рекомендации)
├── sync_configs.py           # актуализация ai-core config.json + импорт clm
├── run_all.py                # единый entrypoint: весь конвейер одной командой
├── ai_core_paths.py          # единые пути ai-core (root, config, каталог, свежесть)
├── filter_config.json        # пороги цен, эталон, параметры скоринга, sync
├── imports/
│   └── chatLanguageModels.json  # сгенерированный VS Code формат (копия для сравнения)
├── templates/
│   └── report_template.html  # Jinja2 шаблон интерактивного отчёта
├── systemd/                  # unit-файлы для автоматизации (раз в 2 дня)
├── data/                     # результаты (raw, filtered, catalog, config_models, recommendations)
└── reports/
    ├── models_report.html    # сгенерированный HTML-отчёт
    └── models_report.md      # Markdown-версия (для чтения без браузера)
```

## Конвейер

```
fetch_models.py ──► data/routerai_models_raw.json (468 моделей)
      │
      ▼
filter_models.py ──► data/filtered_models.json (265 валидных чат-моделей)
      │                cheap: 87 · medium: 41 · premium: 137
      │                (включая vision — изображения на входе)
      ▼
build_report.py ──► data/catalog.json (265)
                    data/config_models.json (формат ai-core)
                    reports/models_report.html (интерактивный + рекомендации)
      │
      ▼
recommend.py ──► data/recommendations.json (скоринг, категории, топ-N)
      │
      ▼
sync_configs.py ──► ai-core/config/config.json (models + ссылки актуализированы)
                    imports/chatLanguageModels.json (VS Code формат)
      │
      ▼
compare_with_config.py ──► отчёт по 265 моделям ai-core (расхождения + рекомендации)
```

## Быстрый старт

```bash
cd /home/dev/projects/routerai-models
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Выгрузка каталога
python3 fetch_models.py

# 2. Валидация и тиринг
python3 filter_models.py

# 3. Каталог + конфиг + HTML/MD-отчёт
python3 build_report.py

# 4. Рекомендации по задачам
python3 recommend.py

# 5. (опционально) Актуализация конфигов — сначала dry-run
python3 sync_configs.py --dry-run
python3 sync_configs.py --apply        # записать config.json + imports/clm
python3 sync_configs.py --apply --apply-clm  # + обновить источник VS Code clm

# 6. Сравнение с ai-core
python3 compare_with_config.py
```

### Весь конвейер одной командой (`run_all.py`)

Единый entrypoint — запускает все шаги в правильном порядке:

```bash
python3 run_all.py                   # полный прогон, sync в dry-run
python3 run_all.py --no-fetch        # без сетевой выгрузки (офлайн)
python3 run_all.py --skip-compare    # без сравнения с ai-core
python3 run_all.py --apply           # + реальная запись config.json + clm
python3 run_all.py --apply --apply-clm  # + источник VS Code clm
```

Эквивалент цепочке `fetch → filter → build_report → recommend →
sync_configs → compare_with_config`, но с остановкой при первой ошибке шага.
Порядок совпадает с сервисом `systemd/routerai-models-fetch.service`.

Отчёты: откройте `reports/models_report.html` в браузере (или через
`python3 -m http.server 8899`), либо читайте `reports/models_report.md` —
обычный Markdown с той же информацией (сводка, эталон, рекомендации, топ-10,
полные таблицы по тирам) без необходимости открывать браузер.

## Валидация и тиры

**Валидная чат-модель** (265 из 468):
- на входе есть `text`, на выходе есть `text`
  (`validation.modality_mode = 'text_in_text_out'` — допускает image/audio/video
  на входе, vision-модели включаются);
- параметр `tools` в `supported_parameters`
- положительные `pricing.prompt` и `pricing.completion`

> Режим строгого отбора `text->text` (только текст без vision) можно вернуть,
> задав `validation.modality_mode = 'text->text'`.

> ⚠ Цены в API routerai.ru выражены в **рублях за 1 токен**. Скрипты переводят
> их в ₽/1M токенов умножением на 10⁶.

**Непересекающиеся тиры** (пороги из `filter_config.json`, по умолчанию):

| Тир | Условие | Кол-во |
|-----|---------|--------|
| `cheap` | prompt < 100 ₽/1M **И** completion < 100 ₽/1M | 87 |
| `premium` | есть `reasoning` **И** (prompt ≥ 100 ИЛИ completion ≥ 100) | 137 |
| `medium` | все остальные валидные | 41 |

**Стоимость типового шага агента**: `prompt_tokens/1M × prompt₽ + completion_tokens/1M × completion₽`
(по умолчанию 10K prompt / 1K completion).

## Формат `data/config_models.json`

Список моделей в формате, совместимом с секцией
`llm.providers.routerai.models` конфига `ai-core/config/config.json`:

```json
{
  "id": "deepseek/deepseek-v4-flash",
  "name": "deepseek/deepseek-v4-flash",
  "url": "https://routerai.ru/api/v1",
  "toolCalling": true,
  "vision": false,
  "maxInputTokens": 838860,
  "maxOutputTokens": 209716,
  "_meta": { "tier": "cheap", "has_reasoning": false, "pricing_rub_per_1m": {...} }
}
```

Поле `_meta` — служебное (не копируется в ai-core). `maxInputTokens`/`maxOutputTokens`
рассчитываются как 80%/20% от `context_length`, **ограниченные сверху потолками
по тирам** (`token_limits` в `filter_config.json`):

| Тир | `maxInputTokens` ≤ | `maxOutputTokens` ≤ |
|-----|--------------------|---------------------|
| `cheap` | 250 000 | 16 000 |
| `medium` | 150 000 | 8 000 |
| `premium` | 100 000 | 8 000 |

Модели с реально небольшим контекстом остаются без изменений (берётся минимум
из оценки и потолка) — потолок лишь «ужимает» модели с гигантским контекстом
до разумного для агентных сценариев значения.

## Сравнение с ai-core (`compare_with_config.py`)

```bash
python3 compare_with_config.py                     # текстовый отчёт
python3 compare_with_config.py --json              # JSON для парсинга
python3 compare_with_config.py --config /path/to/config.json
```

Находит:
- **Устаревшие** модели (есть в конфиге, нет в каталоге API);
- **Новые** кандидаты (есть в API, нет в конфиге) — отсортированы по цене;
- **Расхождения** параметров (`context_length`, `toolCalling`, цены);
- **Рекомендации** fit-to-purpose (из `data/recommendations.json`), если
  сгенерированы `recommend.py`;
- Модели, известные VS Code (`imports/chatLanguageModels.json`).

Файлы `ai-core` не модифицируются — только read-only анализ.

## Рекомендации (`recommend.py`)

Скоринг fit-to-purpose (0–100) по 8 категориям использования:
`default_agent`, `planner`, `reviewer`, `synthesis`, `browser_automation`,
`coding`, `long_context`, `cheap_workhorse`.

```bash
python3 recommend.py                    # → data/recommendations.json + консольный отчёт
python3 recommend.py --top-n 5          # топ-5 по каждой категории
```

Оценка: база 50 + бонус за стоимость (относительно эталона) + бонус за качество
(контекст, tools, structured outputs, reasoning, vision) + бонус за новизну
(свежесть по дате `created` в каталоге: полный бонус до 90 дней, плавный спад
до 730 дней) + бонус кодерам − штраф за устаревшие семейства. Параметры — в
`filter_config.json` → `scoring` (`max_novelty_bonus`, `novelty_window_days`,
`novelty_decay_days`).

## Актуализация конфигов (`sync_configs.py`)

Обновляет `ai-core/config/config.json` (модели + ссылки `routerai:<id>`) и
генерирует `imports/chatLanguageModels.json` (VS Code формат) из актуального
каталога. В конфиги попадает **топ-15 моделей** (настраивается
`filter_config.json` → `sync.selection`), а не весь каталог.

```bash
python3 sync_configs.py --dry-run        # показать дифф, ничего не писать
python3 sync_configs.py --apply          # записать config.json + imports/clm
python3 sync_configs.py --apply --apply-clm  # + обновить источник VS Code clm
```

### Отбор топ-15

1. **Эталон** (`reference_model.id`) включается всегда — рабочая лошадка.
2. Для остальных считается **гибридный score**: сумма баллов модели по 8
   категориям рекомендаций (`data/recommendations.json`) с весами
   (`sync.selection.weights`): `default_agent` и `cheap_workhorse` — основные
   лошадки (×1.0), `reviewer`/`coding` — ×0.8, `planner`/`long_context` — ×0.6,
   `synthesis`/`browser_automation` — ×0.4.
3. **Квоты по тирам** (`sync.selection.quotas`), чтобы были представлены все
   ценовые категории: ~10 cheap + 2 medium + 3 premium (сумма = `total`).
4. Итоговый список сортируется по релевантности: эталон + топ-cheap (рабочие
   лошадки), затем топ-premium (качественные), medium в конце; внутри группы —
   гибридный score по убыванию.

Если `data/recommendations.json` отсутствует (не запускался `recommend.py`) —
фолбэк на старую логику: приоритет по тиру (cheap → medium → premium), внутри —
по цене. `name` моделей — читаемые: из каталога (например
«DeepSeek: DeepSeek V4 Flash 0731», «Z.ai: GLM 5.3»), `:exacto` помечается
скобками, а алиасы `~...-latest` — суффиксом «(latest)», чтобы не путать их в
UI с датированной версией того же семейства
(«DeepSeek: DeepSeek V4 Flash 0731 (latest)»).

- Ссылки вида `routerai:<id>` (default/reviewer/planner/synthesizer/.../llm_model)
  переписываются на ближайшие дешёвые альтернативы, если текущий id исчез из
  каталога.
- Бэкапы не создаются: репозиторий `ai-core` покрыт git.
- Источник VS Code `chatLanguageModels.json` обновляется **только** с флагом
  `--apply-clm` (безопасность).

### Защита записи

`sync_configs.py --apply` **не пишет** конфиги (ни `config.json`, ни clm), если
каталог `data/catalog.json`:
- отсутствует или повреждён (битый JSON);
- пуст (меньше 1 модели — подозрительный ответ API);
- устарел (mtime старше `ai_core.stale_after_hours`, по умолчанию 72 ч).

Это защищает от записи битого/пустого конфига поверх рабочего, если pipeline
(`fetch → filter → build → recommend`) не доработал. В `--dry-run` устаревший
каталог лишь помечается предупреждением, дифф показывается для отладки.

### Единые пути ai-core (`ai_core_paths.py`)

Все скрипты (`sync_configs`, `compare_with_config`, `recommend`) определяют
расположение `ai-core` через общий модуль `ai_core_paths.py`, а не хардкодом:

- `ai_core.project_root` + `ai_core.config_path` в `filter_config.json`
  (по умолчанию `/home/dev/projects/ai-core` + `config/config.json`);
- env-переопределения: `AI_CORE_ROOT`, `AI_CORE_CONFIG` (полный путь к
  config.json), `ROUTERAI_CATALOG` (полный путь к каталогу),
  `ROUTERAI_STALE_HOURS` (лимит свежести каталога).

## Конфигурация (`filter_config.json`)

| Параметр | Описание | По умолчанию |
|----------|----------|--------------|
| `api.base_url` | Базовый URL API | `https://routerai.ru/api/v1` |
| `validation.modality_mode` | Режим фильтра модальности: `text->text` (строго) или `text_in_text_out` (текст на входе/выходе, допускает vision) | `text_in_text_out` |
| `tiers.cheap.max_*` | Пороги cheap, ₽/1M | 100 / 100 |
| `tiers.premium.min_*` | Пороги premium, ₽/1M | 100 / 100 |
| `token_limits.default_context_length` | Контекст по умолчанию (если в каталоге нет) | 128000 |
| `token_limits.max_input_tokens` | Потолки входа по тирам (cheap/medium/premium) | 250K/150K/100K |
| `token_limits.max_output_tokens` | Потолки выхода по тирам (cheap/medium/premium) | 16K/8K/8K |
| `call_params.prompt_tokens` | Prompt типового вызова | 10000 |
| `call_params.completion_tokens` | Completion типового вызова | 1000 |
| `reference_model.id` | Эталонная модель (бейдж ⭐ в отчёте) | `deepseek/deepseek-v4-flash-0731` |
| `scoring.*` | Параметры скоринга рекомендаций (бонусы, новизна, штрафы) | см. файл |
| `sync.selection.total` | Сколько моделей попадает в конфиги | 15 |
| `sync.selection.quotas` | Квоты по тирам (cheap/medium/premium) | 10/2/3 |
| `sync.selection.weights` | Веса категорий для гибридного score | см. файл |
| `ai_core.project_root` | Корень проекта ai-core | `/home/dev/projects/ai-core` |
| `ai_core.config_path` | Путь к config.json относительно корня ai-core | `config/config.json` |
| `ai_core.stale_after_hours` | Макс. возраст каталога (ч), после которого запись запрещена | 72 |
| `sync.*` | Пути clm, параметры отбора моделей | см. файл |

Переменные окружения (опционально, см. `.env.example`): `ROUTERAI_API_URL`,
`ROUTERAI_TIMEOUT`, `AI_CORE_ROOT`, `AI_CORE_CONFIG`, `ROUTERAI_CATALOG`,
`ROUTERAI_STALE_HOURS`.

## Автоматизация (systemd)

Запуск раз в два дня (1, 3, 5, … числа месяца) в 05:30:

```bash
# Пользовательский timer (рекомендуется)
mkdir -p ~/.config/systemd/user
cp systemd/routerai-models-fetch.service systemd/routerai-models-fetch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now routerai-models-fetch.timer
systemctl --user list-timers routerai-models-fetch

# Системный вариант (при желании)
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now routerai-models-fetch.timer
```

Просмотр журнала:

```bash
journalctl --user -u routerai-models-fetch -n 50
```

> Для системного unit укажите своего пользователя в `User=` внутри service-файла.

## Проверка

| Команда | Ожидаемый результат |
|---------|---------------------|
| `python3 fetch_models.py` | «Получено моделей», exit 0 |
| `python3 filter_models.py` | cheap / medium / premium |
| `python3 build_report.py` | Файлы созданы, отчёт открывается без JS-ошибок |
| `python3 recommend.py` | `data/recommendations.json`, категории + топ-N |
| `python3 sync_configs.py --dry-run` | Дифф без записи |
| `python3 compare_with_config.py` | Отчёт по моделям ai-core + рекомендации |
| `python3 run_all.py --no-fetch` | Весь конвейер за один запуск |

## Изоляция и откат

`recommend.py` и `compare_with_config.py` **не изменяют** файлы `ai-core`.
`sync_configs.py` изменяет `ai-core/config/config.json` только с флагом `--apply`
(репозиторий покрыт git — откат через `git checkout`). Для восстановления
каталога достаточно повторно выполнить `fetch_models.py → filter_models.py →
build_report.py`.
