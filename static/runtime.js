(() => {
  const button = document.querySelector('#syncRadar');
  const status = document.querySelector('#radarStatusMessage');
  if (!button) return;

  const pct = document.querySelector('#radarProgressPct');
  const stage = document.querySelector('#radarStage');
  const eta = document.querySelector('#radarEta');
  const bar = document.querySelector('#radarProgressBar');

  let tickBusy = false;
  let driveTimer = null;
  let driveEnabled = false;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  function paintStart(message = 'Связываюсь с Render…') {
    if (pct) pct.textContent = '0%';
    if (stage) stage.textContent = 'Подключаю радар';
    if (eta) eta.textContent = 'жду сервер';
    if (bar) bar.style.width = '0%';
    if (status) status.textContent = message;
  }

  async function jsonPost(url, timeoutMs = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        method: 'POST',
        cache: 'no-store',
        headers: {Accept: 'application/json'},
        signal: controller.signal,
      });
      const text = await response.text();
      const contentType = String(response.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('text/html') || /^\s*</.test(text)) {
        const error = new Error(`Render возвращает служебную страницу вместо API (HTTP ${response.status})`);
        error.status = response.status || 502;
        error.transient = true;
        throw error;
      }
      let data = {};
      if (text.trim()) {
        try { data = JSON.parse(text); }
        catch (_) {
          const error = new Error(`Некорректный JSON от сервера (HTTP ${response.status})`);
          error.status = response.status;
          error.transient = true;
          throw error;
        }
      }
      if (!response.ok) {
        const error = new Error(data?.error || data?.message || `HTTP ${response.status}`);
        error.status = response.status;
        error.transient = [408, 425, 429, 500, 502, 503, 504].includes(response.status);
        throw error;
      }
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') {
        const timeout = new Error('Render не ответил вовремя');
        timeout.transient = true;
        throw timeout;
      }
      if (error instanceof TypeError) error.transient = true;
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async function refreshTruth() {
    try {
      if (typeof loadRadarStatus === 'function') await loadRadarStatus();
      if (typeof loadCandidates === 'function') await loadCandidates();
      if (typeof loadRadar === 'function') await loadRadar();
      if (typeof loadRadarMeta === 'function') await loadRadarMeta();
      if (typeof loadStatus === 'function') await loadStatus();
    } catch (_) {}
  }

  function scheduleDrive(delay = 3500) {
    clearTimeout(driveTimer);
    if (!driveEnabled) return;
    driveTimer = setTimeout(driveOnce, delay);
  }

  async function driveOnce() {
    if (tickBusy) {
      scheduleDrive(1500);
      return;
    }
    tickBusy = true;
    try {
      const data = await jsonPost('/api/radar/tick', 150000);
      driveEnabled = Boolean(data?.active);
      await refreshTruth();
      if (data?.transient_error && status) {
        status.textContent = `Шаг временно не прошёл: ${data.message || 'ошибка'}. Повторяю автоматически…`;
      }
      if (driveEnabled) scheduleDrive(data?.busy ? 1800 : 3200);
      else {
        button.disabled = false;
        button.textContent = 'ЗАПУСТИТЬ ПОИСК';
      }
    } catch (error) {
      // During a Render deploy/swap the edge can return 502 before Flask sees the
      // request. The durable state is already in KVS, so simply retry the tick.
      if (status) status.textContent = `Render переключает instance: ${error.message}. Продолжаю автоматически…`;
      driveEnabled = true;
      scheduleDrive(3500);
    } finally {
      tickBusy = false;
    }
  }

  async function startWithRetry() {
    const deadline = Date.now() + 90000;
    let attempt = 0;
    let lastError = null;

    while (Date.now() < deadline) {
      attempt += 1;
      try {
        if (status) status.textContent = attempt === 1
          ? 'Создаю durable job в Apify KVS…'
          : `Render ещё переключается. Повтор запуска ${attempt}…`;
        return await jsonPost('/api/radar/sync', 25000);
      } catch (error) {
        lastError = error;
        if (!error?.transient && ![502, 503, 504].includes(Number(error?.status || 0))) throw error;
        await sleep(Math.min(7000, 1200 + attempt * 900));
      }
    }
    throw lastError || new Error('Render не стал доступен за 90 секунд');
  }

  async function startRadar() {
    button.disabled = true;
    button.textContent = 'ПОДКЛЮЧАЮСЬ…';
    paintStart();

    try {
      const data = await startWithRetry();
      const runId = data?.run_id || '—';
      if (pct) pct.textContent = '1%';
      if (stage) stage.textContent = data?.resumed ? 'Продолжаю поиск' : 'Поиск принят';
      if (eta) eta.textContent = '≈ 6–10 мин';
      if (bar) bar.style.width = '1%';
      if (status) {
        status.textContent = data?.resumed
          ? `Найден job ${runId}. Продолжаю ровно с сохранённого этапа.`
          : `Job ${runId} сохранён. Теперь поиск идёт короткими restart-safe шагами.`;
      }
      button.textContent = 'ПОИСК ИДЁТ…';
      driveEnabled = true;
      await refreshTruth();
      scheduleDrive(250);
    } catch (error) {
      if (status) status.textContent = `Не удалось подтвердить запуск: ${error.message}. Ничего не считаю запущенным.`;
      button.disabled = false;
      button.textContent = 'ЗАПУСТИТЬ ПОИСК';
    }
  }

  // Capture phase prevents the legacy app.js handler from issuing a duplicate POST.
  button.addEventListener('click', event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    startRadar();
  }, true);

  // One bootstrap tick is enough to recover a KVS job after a Render restart or
  // browser reload. If it is active, the loop keeps driving it; if not, it stops.
  setTimeout(async () => {
    try {
      const data = await jsonPost('/api/radar/tick', 30000);
      driveEnabled = Boolean(data?.active);
      await refreshTruth();
      if (driveEnabled) {
        button.disabled = true;
        button.textContent = 'ПОИСК ИДЁТ…';
        scheduleDrive(1200);
      }
    } catch (_) {
      // Page remains usable; the next explicit start has its own 90s retry window.
    }
  }, 900);
})();
