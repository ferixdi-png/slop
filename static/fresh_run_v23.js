(() => {
  const EMPTY_TOP = '<div class="empty">Новый поиск запущен. Старый TOP очищен — собираю свежие #omni и #veo.</div>';
  const EMPTY_CANDIDATES = '<div class="empty">Новый поиск запущен. Старый пул очищен — жду свежие Reels из #omni и #veo.</div>';
  const EMPTY_META = '<div class="card"><div class="empty">Старая мета очищена. Новая появится после завершения текущего поиска.</div></div>';

  let initialized = false;
  let lastRunId = '';
  let lastResetMarker = '';

  function clearOldRadarDom() {
    const top = document.getElementById('radarRows');
    const candidates = document.getElementById('candidateRows');
    const meta = document.getElementById('radarMeta');
    if (top) top.innerHTML = EMPTY_TOP;
    if (candidates) candidates.innerHTML = EMPTY_CANDIDATES;
    if (meta) meta.innerHTML = EMPTY_META;
  }

  async function pollRunIdentity() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/radar/job', {cache: 'no-store', headers: {Accept: 'application/json'}});
      if (!response.ok) return;
      const job = await response.json();
      const runId = String(job?.run_id || '');
      const reset = job?.result?.fresh_run_reset || job?.fresh_run_reset || '';

      if (!initialized) {
        initialized = true;
        lastRunId = runId;
        lastResetMarker = String(reset || '');
        return;
      }

      if (runId && runId !== lastRunId) {
        clearOldRadarDom();
      }
      lastRunId = runId;
      lastResetMarker = String(reset || lastResetMarker || '');
    } catch (_) {
      // Existing runtime handles reconnects; this helper never owns transport UI.
    }
  }

  // Same-tab UX: clear immediately when the user intentionally starts a new
  // search after the previous one has finished. Server-side V23 is still the
  // source of truth and protects active-run resume from data loss.
  const startButton = document.getElementById('syncRadar');
  if (startButton) {
    startButton.addEventListener('click', () => {
      if (!startButton.disabled && startButton.textContent.includes('ЗАПУСТИТЬ')) {
        clearOldRadarDom();
      }
    }, {capture: true});
  }

  pollRunIdentity();
  setInterval(pollRunIdentity, 2500);
  window.addEventListener('online', pollRunIdentity);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) pollRunIdentity();
  });
})();
