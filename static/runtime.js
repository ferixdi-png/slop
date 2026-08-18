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
  let consecutiveTransportErrors = 0;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const CLIENT_ID = (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function')
    ? globalThis.crypto.randomUUID()
    : `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const LEASE_KEY = 'trend-radar-driver-v19';
  const LEASE_TTL_MS = 12000;

  function readLease() {
    try {
      const value = JSON.parse(localStorage.getItem(LEASE_KEY) || 'null');
      return value && typeof value === 'object' ? value : null;
    } catch (_) {
      return null;
    }
  }

  function claimDriverLease() {
    try {
      const now = Date.now();
      const lease = readLease();
      if (lease && lease.owner !== CLIENT_ID && Number(lease.expires || 0) > now) return false;
      localStorage.setItem(LEASE_KEY, JSON.stringify({owner: CLIENT_ID, expires: now + LEASE_TTL_MS}));
      const confirmed = readLease();
      return Boolean(confirmed && confirmed.owner === CLIENT_ID);
    } catch (_) {
      return true;
    }
  }

  function renewDriverLease() {
    try {
      const lease = readLease();
      if (!lease || lease.owner !== CLIENT_ID) return false;
      localStorage.setItem(LEASE_KEY, JSON.stringify({owner: CLIENT_ID, expires: Date.now() + LEASE_TTL_MS}));
      return true;
    } catch (_) {
      return true;
    }
  }

  function releaseDriverLease() {
    try {
      const lease = readLease();
      if (lease && lease.owner === CLIENT_ID) localStorage.removeItem(LEASE_KEY);
    } catch (_) {}
  }

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

  async function requestJson(url, options = {}, timeoutMs = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        headers: {Accept: 'application/json', ...(options.headers || {})},
        ...options,
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

  const jsonPost = (url, timeoutMs = 30000) => requestJson(url, {method: 'POST'}, timeoutMs);
  const jsonGet = (url, timeoutMs = 15000) => requestJson(url, {method: 'GET'}, timeoutMs);

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
    if (!claimDriverLease()) {
      driveTimer = setTimeout(async () => {
        await refreshTruth();
        scheduleDrive(2200);
      }, 2200);
      return;
    }
    driveTimer = setTimeout(driveOnce, delay);
  }

  async function driveOnce() {
    if (stopRequested) {
      driveEnabled = false;
      releaseDriverLease();
      return;
    }
    if (!claimDriverLease()) {
      scheduleDrive(1800);
      return;
    }
    renewDriverLease();

    if (tickBusy) {
      scheduleDrive(1500);
      return;
    }
    tickBusy = true;
    try {
      const data = await jsonPost('/api/radar/tick', 150000);
      consecutiveTransportErrors = 0;
      renewDriverLease();
      if (stopRequested) {
        driveEnabled = false;
        await refreshTruth();
        releaseDriverLease();
        return;
      }
      driveEnabled = Boolean(data?.active);
      await refreshTruth();

      if (data?.retry_budget_exceeded) {
        driveEnabled = false;
        releaseDriverLease();
        setRunningUi(false);
        if (status) status.textContent = data.message || 'Сервер остановил одинаковую повторяющуюся ошибку.';
        return;
      }

      if (data?.transient_error && status) {
        const repeats = Number(data?.same_error_repeats || 1);
        status.textContent = `Шаг временно не прошёл (${repeats}/${data?.same_error_retry_limit || 6}): ${data.message || 'ошибка'}. Повторяю автоматически…`;
      }
      if (driveEnabled) {
        setRunningUi(true);
        const repeats = Number(data?.same_error_repeats || 0);
        const retryDelay = data?.transient_error ? Math.min(15000, 2800 + repeats * 1700) : (data?.busy ? 1800 : 3200);
        scheduleDrive(retryDelay);
      } else {
        releaseDriverLease();
        setRunningUi(false);
      }
    } catch (error) {
      if (stopRequested) {
        driveEnabled = false;
        releaseDriverLease();
        return;
      }
      consecutiveTransportErrors += 1;

      if (!error?.transient) {
        driveEnabled = false;
        releaseDriverLease();
        setRunningUi(false);
        if (status) status.textContent = `Автоповтор остановлен: ${error.message}. Ошибка не выглядит временной.`;
        return;
      }

      if (consecutiveTransportErrors >= 8) {
        driveEnabled = false;
        releaseDriverLease();
        setRunningUi(false);
        if (status) status.textContent = `Связь с Render не восстановилась после ${consecutiveTransportErrors} попыток. Автоповтор приостановлен, чтобы не создавать бесконечный цикл. Нажми «ЗАПУСТИТЬ ПОИСК» — сервер продолжит сохранённый run.`;
        return;
      }

      if (status) status.textContent = `Render временно недоступен: ${error.message}. Попытка ${consecutiveTransportErrors}/8…`;
      driveEnabled = true;
      setRunningUi(true);
      scheduleDrive(Math.min(16000, 2500 + consecutiveTransportErrors * 1800));
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
          ? 'Создаю durable job…'
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
    consecutiveTransportErrors = 0;
    claimDriverLease();
    button.disabled = true;
    button.textContent = 'ПОДКЛЮЧАЮСЬ…';
    if (stopButton) stopButton.classList.add('hidden');
    paintStart();

    try {
      const data = await startWithRetry();

      // A pending STOP always wins over a new START, even from another tab.
      if (data?.stop_pending || data?.accepted === false) {
        releaseDriverLease();
        if (stage) stage.textContent = 'Завершаю предыдущую остановку';
        if (eta) eta.textContent = 'несколько секунд';
        if (status) status.textContent = data?.message || 'Предыдущий поиск ещё останавливается. Сначала подтверждаю STOP.';
        await stopRadar();
        return;
      }

      const runId = data?.run_id || '—';
      if (pct) pct.textContent = '1%';
      if (stage) stage.textContent = data?.migrated ? 'Обновляю старый поиск' : (data?.resumed ? 'Продолжаю поиск' : 'Поиск принят');
      if (eta) eta.textContent = '≈ 6–10 мин';
      if (bar) bar.style.width = '1%';
      if (status) status.textContent = data?.message || `Run ${runId} сохранён.`;
      driveEnabled = true;
      setRunningUi(true);
      await refreshTruth();
      scheduleDrive(250);
    } catch (error) {
      if (status) status.textContent = `Не удалось подтвердить запуск: ${error.message}. Ничего не считаю запущенным.`;
      driveEnabled = false;
      releaseDriverLease();
      setRunningUi(false);
    }
  }

  async function confirmStopUntilTerminal(firstData) {
    let data = firstData || {};
    for (let attempt = 0; attempt < 24; attempt += 1) {
      if (data?.cancelled || data?.already_stopped || data?.active === false) return data;
      await sleep(1500);
      try {
        data = await jsonPost('/api/radar/stop', 12000);
      } catch (error) {
        if (!error?.transient) throw error;
      }
    }
    return data;
  }

  async function stopRadar() {
    stopRequested = true;
    driveEnabled = false;
    clearTimeout(driveTimer);
    releaseDriverLease();

    button.disabled = true;
    button.textContent = 'ОСТАНАВЛИВАЮ…';
    if (stopButton) {
      stopButton.classList.remove('hidden');
      stopButton.disabled = true;
      stopButton.textContent = 'ОСТАНАВЛИВАЮ…';
    }
    if (stage) stage.textContent = 'Останавливаю поиск';
    if (eta) eta.textContent = 'подтверждаю stop-marker';
    if (status) status.textContent = 'Фиксирую принудительную остановку. Уже начатый атомарный шаг может закончиться, но следующий не запустится.';

    try {
      const first = await jsonPost('/api/radar/stop', 15000);
      const data = await confirmStopUntilTerminal(first);
      await refreshTruth();

      if (data?.active === true && !data?.cancelled) {
        if (status) status.textContent = 'Stop-marker сохранён, но текущий внешний шаг ещё не завершился. Сервер не запустит следующий шаг; обнови статус через несколько секунд.';
        if (stopButton) {
          stopButton.disabled = false;
          stopButton.textContent = 'ПРОВЕРИТЬ ОСТАНОВКУ';
        }
        button.disabled = true;
        button.textContent = 'ОСТАНОВКА ЗАПРОШЕНА';
        return;
      }

      if (pct) pct.textContent = '0%';
      if (stage) stage.textContent = 'Поиск остановлен';
      if (eta) eta.textContent = 'ОСТАНОВЛЕНО';
      if (bar) bar.style.width = '0%';
      if (status) status.textContent = data?.message || 'Поиск принудительно остановлен. Можно запускать новый run.';
      setRunningUi(false);
    } catch (error) {
      await refreshTruth();
      if (status) status.textContent = `Не удалось подтвердить остановку: ${error.message}. Stop можно безопасно нажать повторно.`;
      button.disabled = true;
      button.textContent = 'ОСТАНОВКА НЕ ПОДТВЕРЖДЕНА';
      if (stopButton) {
        stopButton.classList.remove('hidden');
        stopButton.disabled = false;
        stopButton.textContent = 'ОСТАНОВИТЬ ПОВТОРНО';
      }
    }
  }

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

  setTimeout(async () => {
    try {
      const data = await jsonGet('/api/radar/job', 12000);
      if (stopRequested) return;
      driveEnabled = Boolean(data?.active);
      await refreshTruth();
      if (driveEnabled) {
        setRunningUi(true);
        scheduleDrive(900);
      } else {
        setRunningUi(false);
      }
    } catch (_) {}
  }, 900);

  window.addEventListener('beforeunload', releaseDriverLease);
})();
