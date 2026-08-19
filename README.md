# ПОИСК ТРЕНДОВ — FINAL V37

Личная production-панель для поиска свежих коротких вирусных механик в **Instagram Reels, TikTok и YouTube Shorts**, ранжирования роликов по реальному momentum и сборки готового production-пакета по выбранному исходнику.

## Финальный scope

Радар работает только с последними **14 днями** и пятью строгими post-level тегами:

```text
#omni  #veo  #veo3  #ai  #ии
```

Источник поиска сам по себе не считается доказательством тега: нужный hashtag должен реально присутствовать в данных самого поста. Поддерживаются только:

```text
Instagram Reels
TikTok
YouTube Shorts
```

Известная длительность исходника должна быть **1.00–15.05 сек**. До **10.05 сек** ролик можно адаптировать в исходном тайминге; 10.05–15.05 сек помечаются для естественной сборки в **10.00 сек**. Подтверждённые static image / screenshot / poster / slideshow / почти неподвижные видео остаются hard reject.

## Как теперь формируется TOP

Финальная логика — **momentum first, Gemini enrichment second**.

Видимость сильного кандидата определяется числовым трендовым сигналом: текущей скоростью набора, свежестью, просмотрами и cross-run историей роста. Если есть повторное измерение, TOP предпочитает **measured growth/hour**; если истории ещё нет — используется текущий **views/hour**.

Gemini больше не является разрушительным фильтром выдачи:

- нет речи → ролик остаётся в TOP, production предлагает добавить русскую речь;
- ролик длиннее 10 сек → остаётся и получает адаптацию под 10 секунд;
- tutorial/talking-head → механику можно переупаковать;
- временная AI/media ошибка → сильный числовой кандидат остаётся для ручной проверки;
- hard reject сохраняется только для объективно неподходящего media/duration/motion.

Автоматическая AI-разметка ограничена лучшими кандидатами и используется как **enrichment only**.

## Momentum и fresh run

`radar_multiplatform_v28` — единственный владелец расчёта five-tag / 14-day momentum и таблицы `radar_momentum_history`.

`radar_momentum_v34` не создаёт параллельную историю. Перед финальной сборкой TOP он запускает один актуальный refresh, сохраняет checkpoint существующей истории и сортирует только строки текущего screening profile.

Новый поиск очищает старую видимую выдачу, но не уничтожает скрытую momentum-историю, поэтому следующий run может измерить реальное ускорение между запусками.

## Ручной запуск — абсолютное правило

Платная работа начинается или продолжается только после явного действия пользователя.

Открытие сайта, F5, новая вкладка, повторный GET, restart/deploy Render **не имеют права** сами запускать или продолжать paid discovery. Durable job может существовать в KVS, но после нового browser/server lifecycle он остаётся paused до явного **Start / Continue**.

Driver-token живёт только в памяти текущего процесса и не сохраняется в KVS/SQLite/browser storage.

## Бюджет

Один полный discovery ограничен на уровне Actor-run:

```text
Instagram  max 300 items  / hard cap $0.85
TikTok     max 300 items  / hard cap $1.15
YouTube    max 300 items  / hard cap $0.80
------------------------------------------
Apify discovery hard cap              $2.80
Reserved headroom                     $2.20
Hard target полного поиска            $5.00
```

На один тег/платформу запрашивается максимум 60 результатов: до 300 на платформу / 900 raw суммарно.

Автоматический screening не запускает дополнительные paid media-refresh Actors после discovery. Ручной on-demand refresh выбранного ролика ограничен:

```text
maxItems=1
maxTotalChargeUsd=$0.12
```

Перед каждым платным discovery-start система проверяет durable cloud mirror. Неоднозначный timeout/connection error после Actor start не приводит к слепому повторному запуску: сначала выполняется adoption/quarantine уже возможного run.

## Production-пакет выбранного ролика

Любой текущий broad-eligible кандидат можно отправить в ручной production-анализ, даже если старый semantic verdict был `ai_match=0`.

Итоговый пакет содержит:

```text
0. Director / reconstruction plan
1. PHOTO / Frame 0 prompt
2. Compliance / continuity map
3. VIDEO prompt
4. Audio / Russian speech / pronunciation
5. Publication block
6. CapCut overlay plan
```

Production pipeline делает forensic-разбор видео, строит пакет, выполняет QA/repair и фиксирует generation target. Повторный одинаковый анализ кешируется, а параллельный дубль одного источника блокируется singleflight-защитой.

## Media reliability

Для выбранного ролика используется трёхплатформенный manual downloader.

Приватные Apify Key-Value Store media URL скачиваются сервером с Bearer-auth только для `api.apify.com`. Токен не помещается в URL/БД/UI/логи и не пересылается на сторонний redirect-host. При 401/403 допускается ephemeral signed URL только для конкретной KVS-записи.

Сохраняются V30 media-защиты:

- только public HTTPS;
- DNS/IP SSRF validation;
- повторная проверка redirect target;
- MIME/HTML/JSON rejection;
- лимит размера;
- fail-closed motion gate.

## Durable state

SQLite — локальный working store. Критичное состояние радара, snapshots и momentum checkpoint зеркалируются через Apify KVS с локальным SQLite fallback.

Fresh-run guard не позволяет старому snapshot воскресить предыдущий TOP поверх нового run.

## Frontend

Production UI — один self-contained V33/V34 HTML runtime с V35 manual-start patch.

Нет production-зависимости от `/static/`, MutationObserver-слоёв или нескольких параллельных JS runtimes. Старые static-файлы могут оставаться только как пассивные compatibility/debug artifacts и не подключаются Gunicorn при старте.

## Render

`render.yaml` запускает один Gunicorn worker с потоками. Render health check использует:

```text
/healthz
```

`/healthz` не зависит от SQLite, Gemini или Apify и предназначен именно для process/liveness проверки.

Для production нужны только два внешних секрета:

```text
GEMINI_API_KEY=...
APIFY_API_TOKEN=...
```

`SECRET_KEY` Render создаёт автоматически.

## Модель Gemini

Проект намеренно фиксирует одну стабильную low-cost multimodal модель для screening и production pipeline:

```text
gemini-3.1-flash-lite
```

Model ID зафиксирован в коде, чтобы старые Render env-переменные не могли незаметно поменять экономику или поведение production.

## Локальный запуск

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
python app.py
```

Открыть:

```text
http://localhost:5000
```

## Что проверяет CI

Финальный набор regression/smoke проверяет в том числе:

- полный Python compile;
- Instagram + TikTok + YouTube scope;
- 5 тегов / 14 дней;
- hard budget и Actor start caps;
- durable preflight/adoption;
- broad TOP 50–100;
- measured momentum и current-profile isolation;
- V35 manual-start без POST на page load;
- real Chromium + Gunicorn;
- V36 private KVS media auth/fallback;
- PHOTO/VIDEO/Audio/CapCut production schema;
- self-contained frontend без `/static/`;
- Render `/healthz` contract.
