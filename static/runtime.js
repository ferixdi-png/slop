(() => {
  const button = document.querySelector('#syncRadar');
  const stopButton = document.querySelector('#stopRadar');
  const status = document.querySelector('#radarStatusMessage');
  if (!button) return;

  const pct = document.querySelector('#radarProgressPct');
  const stage = document.querySelector('#radarStage');
  const eta = document.querySelector('#radarEta');
  const bar = document.querySelector('#radarProgressBar');

  let tickBusy = false;
  let driveTimer = null;
  let driveEnabled = false;
  let stopRequested = false;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  function setRunningUi(running) {
    button.disabled = Boolean(running);
    button.textContent = running ? 'ПОИСК ИДЁТ…' : 'ЗАПУСТИТЬ ПОИСК';
    if (stopButton) {
      stopButton.classList.toggle('hidden', !running);
      stopButton.disabled = false;
      stopButton.textContent = 'ОСТАНОВИТЬ ПОИСК';
    }
  }

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
    if (!driveEnabled || stopRequested) return;
    driveTimer = setTimeout(driveOnce, delay);
  }

  async function driveOnce() {
    if (stopRequested) {
      driveEnabled = false;
      return;
    }
    if (tickBusy) {
      scheduleDrive(1500);
      return;
    }
    tickBusy = true;
    try {
      const data = await jsonPost('/api/radar/tick', 150000);
      if (stopRequested) {
        driveEnabled = false;
        await refreshTruth();
        return;
      }
      driveEnabled = Boolean(data?.active);
      await refreshTruth();
      if (data?.transient_error && status) {
        status.textContent = `Шаг временно не прошёл: ${data.message || 'ошибка'}. Повторяю автоматически…`;
      }
      if (driveEnabled) {
        setRunningUi(true);
        scheduleDrive(data?.busy ? 1800 : 3200);
      } else {
        setRunningUi(false);
      }
    } catch (error) {
      if (stopRequested) {
        driveEnabled = false;
        return;
      }
      // During a Render deploy/swap the edge can return 502 before Flask sees the
      // request. The durable state is already in KVS, so simply retry the tick.
      if (status) status.textContent = `Render переключает instance: ${error.message}. Продолжаю автоматически…`;
      driveEnabled = true;
      setRunningUi(true);
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
    stopRequested = false;
    button.disabled = true;
    button.textContent = 'ПОДКЛЮЧАЮСЬ…';
    if (stopButton) stopButton.classList.add('hidden');
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
      driveEnabled = true;
      setRunningUi(true);
      await refreshTruth();
      scheduleDrive(250);
    } catch (error) {
      if (status) status.textContent = `Не удалось подтвердить запуск: ${error.message}. Ничего не считаю запущенным.`;
      driveEnabled = false;
      setRunningUi(false);
    }
  }

  async function stopRadar() {
    stopRequested = true;
    driveEnabled = false;
    clearTimeout(driveTimer);

    button.disabled = true;
    button.textContent = 'ОСТАНАВЛИВАЮ…';
    if (stopButton) {
      stopButton.classList.remove('hidden');
      stopButton.disabled = true;
      stopButton.textContent = 'ОСТАНАВЛИВАЮ…';
    }
    if (stage) stage.textContent = 'Останавливаю поиск';
    if (eta) eta.textContent = 'жду текущий шаг';
    if (status) status.textContent = 'Принудительно останавливаю durable job и активные источники. Текущий атомарный шаг может завершиться перед остановкой.';

    try {
      const data = await jsonPost('/api/radar/stop', 205000);
      await refreshTruth();

      if (data?.stop_pending) {
        if (status) status.textContent = data.message || 'Текущий шаг ещё завершается. Нажми остановку повторно через несколько секунд.';
        if (stopButton) {
          stopButton.disabled = false;
          stopButton.textContent = 'ОСТАНОВИТЬ ПОВТОРНО';
        }
        button.disabled = true;
        button.textContent = 'ОСТАНОВКА ЗАПРОШЕНА';
        return;
      }

      driveEnabled = false;
      if (pct) pct.textContent = '0%';
      if (stage) stage.textContent = 'Поиск остановлен';
      if (eta) eta.textContent = 'ОСТАНОВЛЕНО';
      if (bar) bar.style.width = '0%';
      if (status) status.textContent = data?.message || 'Поиск принудительно остановлен. Можно запускать новый run.';
      setRunningUi(false);
    } catch (error) {
      await refreshTruth();
      if (status) status.textContent = `Не удалось подтвердить остановку: ${error.message}. Нажми «ОСТАНОВИТЬ ПОИСК» ещё раз.`;
      button.disabled = true;
      button.textContent = 'ПОИСК ИДЁТ…';
      if (stopButton) {
        stopButton.classList.remove('hidden');
        stopButton.disabled = false;
        stopButton.textContent = 'ОСТАНОВИТЬ ПОИСК';
      }
    }
  }

  // Capture phase prevents the legacy app.js handler from issuing a duplicate POST.
  button.addEventListener('click', event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    startRadar();
  }, true);

  if (stopButton) {
    stopButton.addEventListener('click', event => {
      event.preventDefault();
      event.stopImmediatePropagation();
      stopRadar();
    }, true);
  }

  // One bootstrap tick is enough to recover a KVS job after a Render restart or
  // browser reload. If it is active, the loop keeps driving it; if not, it stops.
  setTimeout(async () => {
    try {
      const data = await jsonPost('/api/radar/tick', 30000);
      if (stopRequested) return;
      driveEnabled = Boolean(data?.active);
      await refreshTruth();
      if (driveEnabled) {
        setRunningUi(true);
        scheduleDrive(1200);
      } else {
        setRunningUi(false);
      }
    } catch (_) {
      // Page remains usable; the next explicit start has its own 90s retry window.
    }
  }, 900);
})();