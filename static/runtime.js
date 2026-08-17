(() => {
  const button = document.querySelector('#syncRadar');
  const status = document.querySelector('#radarStatusMessage');
  if (!button) return;

  const pct = document.querySelector('#radarProgressPct');
  const stage = document.querySelector('#radarStage');
  const eta = document.querySelector('#radarEta');
  const bar = document.querySelector('#radarProgressBar');

  async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        ...options,
        signal: controller.signal,
        headers: {
          Accept: 'application/json',
          ...(options.headers || {}),
        },
      });
      const text = await response.text();
      const contentType = String(response.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('text/html') || /^\s*</.test(text)) {
        throw new Error(`Render вернул HTML вместо API-ответа (HTTP ${response.status}). Дождись завершения deploy и повтори запуск.`);
      }
      let data = null;
      if (text.trim()) {
        try { data = JSON.parse(text); }
        catch (_) { throw new Error(`Некорректный JSON от сервера (HTTP ${response.status}).`); }
      }
      if (!response.ok) {
        throw new Error(data?.error || data?.message || `HTTP ${response.status}`);
      }
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new Error(`Сервер не ответил на запуск за ${Math.round(timeoutMs / 1000)} сек. Открой Render → Logs и найди [RADAR][...][launch].`);
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function paintVerifiedLaunch(data) {
    if (pct) pct.textContent = '1%';
    if (stage) stage.textContent = 'Запускаю радар';
    if (eta) eta.textContent = '≈ 6 мин';
    if (bar) bar.style.width = '1%';
    if (status) {
      const run = data?.run_id ? ` Run ID: ${data.run_id}.` : '';
      status.textContent = `${data?.message || 'Команда подтверждена живым worker.'}${run} Подробности — Render → Logs.`;
    }
  }

  async function refreshLaunchStateSeveralTimes() {
    const delays = [300, 1200, 2500, 5000];
    for (const delay of delays) {
      setTimeout(() => {
        if (typeof loadRadarStatus === 'function') loadRadarStatus();
        if (typeof loadCandidates === 'function') loadCandidates();
      }, delay);
    }
  }

  async function startRadar() {
    button.disabled = true;
    button.textContent = 'ПРОВЕРЯЮ ЗАПУСК…';
    if (status) status.textContent = 'Отправляю команду и жду подтверждения фонового worker…';

    try {
      const data = await fetchJsonWithTimeout('/api/radar/sync', {method: 'POST'}, 15000);
      if (!data?.started || data?.runtime?.worker_alive !== true) {
        throw new Error(`Сервер ответил, но не подтвердил живой worker. Run ID: ${data?.run_id || 'не получен'}.`);
      }

      button.textContent = 'ПОИСК ИДЁТ…';
      paintVerifiedLaunch(data);
      refreshLaunchStateSeveralTimes();
    } catch (error) {
      button.disabled = false;
      button.textContent = 'ЗАПУСТИТЬ ПОИСК';
      if (pct) pct.textContent = '0%';
      if (stage) stage.textContent = 'Поиск не запущен';
      if (eta) eta.textContent = 'ОШИБКА';
      if (bar) bar.style.width = '0%';
      if (status) status.textContent = `Не удалось запустить: ${error.message}`;
    }
  }

  // Capture phase guarantees that only this launcher handles the click.
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    startRadar();
  }, true);
})();
