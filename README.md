# ПОИСК ТРЕНДОВ — V30

Личная панель для поиска свежих коротких вирусных механик в Instagram Reels, TikTok и YouTube Shorts, отбора реально набирающих обороты роликов и сборки production-пакета по выбранному исходнику.

## Финальный scope

Радар ищет только посты за последние **14 дней** по пяти строгим post-level тегам:

```text
#omni  #veo  #veo3  #ai  #ии
```

Источник поиска сам по себе не является доказательством тега. Нужный hashtag должен реально присутствовать в данных самого поста. Поддерживаемые платформы: Instagram Reels, TikTok и YouTube Shorts.

После discovery локально проверяются дата, длительность и движение. Static image, screenshot, quote card, poster, slideshow и почти неподвижные ролики не проходят. Затем Gemini обязан услышать реальную речь и подтвердить воспроизводимый тайминг. Для исходника до 10.05 сек сохраняется его фактическая длительность; ролик 10.05–15.05 сек проходит только если обязательные реплики, действия и реакцию можно естественно уложить ровно в 10 секунд без ускоренной речи.

## Бюджет V30

Один полный discovery ограничен на уровне Actor-run:

```text
Instagram  max 300 items  / hard cap $0.85
TikTok     max 300 items  / hard cap $1.15
YouTube    max 300 items  / hard cap $0.80
------------------------------------------
Apify discovery hard cap              $2.80
```

На один тег/платформу запрашивается максимум 60 результатов, поэтому верхняя граница discovery — 900 raw объектов. На speech/timing screening идут максимум 150 лучших кандидатов; дополнительно существует persistent hard guard в 180 automatic AI ticks с учётом retry allowance. Целевой общий бюджет полного автоматического поиска остаётся **меньше $5**.

Автоматический поиск не запускает дополнительные paid-refresh Actors после discovery. Ручной on-demand refresh для выбранного ролика ограничен `maxItems=1` и `$0.12` на Actor-run.

## Что закрыл V30 после полного аудита

1. **Durable paid preflight.** Платный discovery не стартует, если недоступно Apify KVS-зеркало состояния.
2. **Ambiguous Actor start quarantine.** После timeout/connection-сбоя система сначала пытается найти и принять уже созданный run; слепой повторный платный старт запрещён.
3. **Manual refresh hard cap.** Любой on-demand media refresh ограничен одним результатом и `$0.12`.
4. **Production analysis cache.** Одинаковый повторный разбор одного источника с тем же production profile возвращается из кеша.
5. **Singleflight analysis.** Параллельный дубль анализа одного и того же ролика получает 409 вместо второй Gemini-цепочки.
6. **Persistent screening budget.** Automatic Gemini screening не может бесконечно ретраиться после сбоев.
7. **Правильные snapshots.** Snapshot покрывает реальные 14 дней и до 1000 radar rows; restore умеет дополнять частично восстановленную SQLite.
8. **Safe media download.** Только public HTTPS, повторная проверка redirect-целей, лимит размера и отказ от HTML/JSON вместо видео.
9. **Motion gate fail-closed.** Если локальная motion-проверка не работает, кандидат не отправляется в Gemini; static/slideshow — hard reject.
10. **Mutation/rerun guards.** Cross-site mutation блокируется, а случайный мгновенный повтор полного run после завершения debounce-ится.

## Ranking

Финальный TOP сортируется по текущему momentum: скорость набора, свежесть, подтверждённые просмотры и накопленная история ускорения. История momentum хранится отдельно от видимого TOP, поэтому fresh run может очищать выдачу, не уничтожая базу для сравнения между запусками.

## Разбор выбранного ролика

После выбора подходящего ролика сервис собирает production-пакет:

0. режиссёрское решение;
1. Frame 0 prompt;
2. compliance/check map;
3. video prompt;
4. аудио, речь и произношение;
5. публикация.

Production-реконструкция сохраняет роли, порядок событий, speaker ownership, object continuity, исходную механику и тайминг, но переводит визуал в физически правдоподобную smartphone live-action подачу.

## Ключи

Нужны только:

```text
GEMINI_API_KEY=...
APIFY_API_TOKEN=...
```

Остальные рабочие значения имеют безопасные defaults. См. `.env.example`.

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

Открыть `http://localhost:5000`.

## Render

`render.yaml` поднимает один Gunicorn worker с потоками и длинным timeout для внешних API. Health/liveness probe V30 не зависит от SQLite и внешних API. На Render достаточно добавить `GEMINI_API_KEY` и `APIFY_API_TOKEN`; `SECRET_KEY` создаётся автоматически.

SQLite остаётся локальным working store, а критичное radar state/momentum зеркалируется через Apify KVS. Это позволяет не повторять платную работу только из-за замены Render instance.
