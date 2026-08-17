(() => {
  const button = document.querySelector('#syncRadar');
  const status = document.querySelector('#radarStatusMessage');
  if (!button) return;

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

  async function startRadar() {
    button.disabled = true;
    button.textContent = 'ОТПРАВЛЯЮ…';
    if (status) status.textContent = 'Отправляю команду запуска серверу…';

    try {
      const data = await fetchJsonWithTimeout('/api/radar/sync', {method: 'POST'}, 15000);
      button.textContent = 'ПОИСК ИДЁТ…';
      if (status) {
        const run = data?.run_id ? ` Run ID: ${data.run_id}.` : '';
        status.textContent = `${data?.message || 'Команда принята. Радар запущен.'}${run} Подробности — Render → Logs.`;
      }
      setTimeout(() => {
        if (typeof loadRadarStatus === 'function') loadRadarStatus();
        if (typeof loadCandidates === 'function') loadCandidates();
      }, 250);
    } catch (error) {
      button.disabled = false;
      button.textContent = 'ЗАПУСТИТЬ ПОИСК';
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
