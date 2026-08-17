(() => {
  const button = document.querySelector('#syncRadar');
  const status = document.querySelector('#radarStatusMessage');
  if (!button) return;

  const pct = document.querySelector('#radarProgressPct');
  const stage = document.querySelector('#radarStage');
  const eta = document.querySelector('#radarEta');
  const bar = document.querySelector('#radarProgressBar');

  async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 870000) {
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
        throw new Error(`Render вернул HTML вместо API-ответа (HTTP ${response.status}). Проверь Render → Logs.`);
      }
      let data = null;
      if (text.trim()) {
        try { data = JSON.parse(text); }
        catch (_) { throw new Error(`Некорректный JSON от сервера (HTTP ${response.status}).`); }
      }
      if (!response.ok) {
        const error = new Error(data?.error || data?.message || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new Error('Поиск превысил безопасный лимит ожидания браузера. Проверь текущий статус и Render → Logs; сервер мог продолжить работу до своего timeout.');
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function paintImmediateStart() {
    if (pct) pct.textContent = '1%';
    if (stage) stage.textContent = 'Запускаю радар';
    if (eta) eta.textContent = '≈ 8–12 мин';
    if (bar) bar.style.width = '1%';
    if (status) status.textContent = 'Команда отправлена. Один HTTP-запрос выполняет весь радар; этапы обновляются автоматически.';
  }

  async function startRadar() {
    button.disabled = true;
    button.textContent = 'ПОИСК ИДЁТ…';
    paintImmediateStart();

    // app.js continues polling /api/radar/status every 4 seconds while this long
    // POST stays open, so progress, candidates and TOP update during the request.
    try {
      const data = await fetchJsonWithTimeout('/api/radar/sync', {method: 'POST'}, 870000);
      if (status) {
        status.textContent = data?.completed
          ? `Поиск завершён. Run ID: ${data?.run_id || '—'}.`
          : 'Запрос завершён. Обновляю результаты.';
      }
      if (typeof refreshEverything === 'function') await refreshEverything();
    } catch (error) {
      // 409 means another tab/request already runs the radar. Do not paint this as
      // a dead failure: refresh server truth and keep the UI aligned with it.
      if (Number(error?.status || 0) === 409) {
        if (status) status.textContent = error.message;
        if (typeof loadRadarStatus === 'function') await loadRadarStatus();
      } else {
        if (status) status.textContent = `Поиск остановлен: ${error.message}`;
        if (typeof loadRadarStatus === 'function') await loadRadarStatus();
      }
    } finally {
      // Server status is authoritative; loadRadarStatus will immediately disable
      // the button again if a request is genuinely still running.
      button.disabled = false;
      button.textContent = 'ЗАПУСТИТЬ ПОИСК';
      if (typeof loadRadarStatus === 'function') await loadRadarStatus();
    }
  }

  // Capture phase guarantees that the old app.js click handler cannot also fire.
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    startRadar();
  }, true);
})();
