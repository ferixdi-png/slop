(() => {
  const button = document.querySelector('#syncRadar');
  const status = document.querySelector('#radarStatusMessage');
  if (!button) return;

  const pct = document.querySelector('#radarProgressPct');
  const stage = document.querySelector('#radarStage');
  const eta = document.querySelector('#radarEta');
  const bar = document.querySelector('#radarProgressBar');

  function paintStart(message = 'Создаю устойчивый radar job…') {
    if (pct) pct.textContent = '1%';
    if (stage) stage.textContent = 'Запускаю радар';
    if (eta) eta.textContent = '≈ 6–10 мин';
    if (bar) bar.style.width = '1%';
    if (status) status.textContent = message;
  }

  async function postStart() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch('/api/radar/sync', {
        method: 'POST',
        cache: 'no-store',
        headers: {Accept: 'application/json'},
        signal: controller.signal,
      });
      const text = await response.text();
      const contentType = String(response.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('text/html') || /^\s*</.test(text)) {
        throw new Error(`Render вернул HTML вместо API-ответа (HTTP ${response.status})`);
      }
      let data = {};
      if (text.trim()) {
        try { data = JSON.parse(text); }
        catch (_) { throw new Error(`Некорректный JSON от сервера (HTTP ${response.status})`); }
      }
      if (!response.ok) {
        const error = new Error(data?.error || data?.message || `HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  async function refreshTruth() {
    try {
      if (typeof loadRadarStatus === 'function') await loadRadarStatus();
      if (typeof loadCandidates === 'function') await loadCandidates();
      if (typeof loadRadar === 'function') await loadRadar();
    } catch (_) {}
  }

  async function startRadar() {
    button.disabled = true;
    button.textContent = 'ЗАПУСКАЮ…';
    paintStart();

    try {
      const data = await postStart();
      const runId = data?.run_id || '—';
      if (status) {
        status.textContent = data?.resumed
          ? `Найден незавершённый поиск ${runId}. Продолжаю его, повторно Actor-ы не запускаю.`
          : `Поиск принят. Run ID: ${runId}. Job сохранён в Apify KVS; Render может перезапуститься без потери поиска.`;
      }
      await refreshTruth();
    } catch (error) {
      if (Number(error?.status || 0) === 409) {
        if (status) status.textContent = error.message;
      } else {
        // A 502 can happen exactly while Render swaps/restarts an instance. The
        // launch may already have been persisted, so never call it "stopped" here.
        if (status) status.textContent = `Связь с Render оборвалась: ${error.message}. Проверяю сохранённый job автоматически…`;
      }
      setTimeout(refreshTruth, 1500);
      setTimeout(refreshTruth, 4000);
    } finally {
      setTimeout(async () => {
        await refreshTruth();
        // loadRadarStatus is authoritative and will disable the button again when
        // the persistent job is actually active.
        if (!button.disabled || button.textContent === 'ЗАПУСКАЮ…') {
          button.disabled = false;
          button.textContent = 'ЗАПУСТИТЬ ПОИСК';
          await refreshTruth();
        }
      }, 1200);
    }
  }

  // Capture phase prevents the legacy app.js click handler from making a second POST.
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    startRadar();
  }, true);
})();
