(() => {
  const $local = (s) => document.querySelector(s);

  async function fetchJsonTimeout(url, options = {}, timeoutMs = 15000) {
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
        throw new Error(`Render вернул HTML вместо API-ответа (HTTP ${response.status}). Сервис, вероятно, перезапускается.`);
      }
      let data = null;
      if (text.trim()) {
        try { data = JSON.parse(text); }
        catch (_) { throw new Error(`Некорректный JSON от сервера (HTTP ${response.status})`); }
      }
      if (!response.ok) {
        throw new Error(data?.error || data?.message || `HTTP ${response.status}`);
      }
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new Error(`Сервер не ответил за ${Math.round(timeoutMs / 1000)} сек. Проверь журнал ниже и Render Logs.`);
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function formatLogTime(value) {
    try {
      return new Date(value).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    } catch (_) { return '—'; }
  }

  async function loadRadarLogs() {
    const host = $local('#radarLogs');
    if (!host) return;
    try {
      const rows = await fetchJsonTimeout('/api/radar/logs?limit=120', {}, 8000);
      if (!Array.isArray(rows) || !rows.length) {
        host.innerHTML = '<div class="log-empty">Журнал пуст. После запуска здесь появятся все этапы.</div>';
        return;
      }
      host.innerHTML = rows.map(row => {
        const level = String(row.level || 'INFO').toLowerCase();
        const stage = row.stage ? `<span class="log-stage">${escapeHtml(row.stage)}</span>` : '';
        let details = '';
        if (row.details && typeof row.details === 'object') {
          const compact = Object.entries(row.details)
            .filter(([,v]) => v !== '' && v !== null && v !== undefined)
            .slice(0, 8)
            .map(([k,v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
            .join(' · ');
          if (compact) details = `<div class="log-details">${escapeHtml(compact)}</div>`;
        }
        return `<div class="log-row log-${level}">
          <span class="log-time">${formatLogTime(row.created_at)}</span>
          <span class="log-level">${escapeHtml(row.level || 'INFO')}</span>
          ${stage}
          <div class="log-message">${escapeHtml(row.message || '')}${details}</div>
        </div>`;
      }).join('');
      host.scrollTop = host.scrollHeight;
    } catch (e) {
      if (!host.querySelector('.log-row')) {
        host.innerHTML = `<div class="log-empty">Журнал временно недоступен: ${escapeHtml(e.message)}</div>`;
      }
    }
  }

  async function robustStartRadar() {
    const btn = $local('#syncRadar');
    const status = $local('#radarStatusMessage');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'ОТПРАВЛЯЮ КОМАНДУ…';
    }
    if (status) status.textContent = 'Отправляю команду запуска серверу…';

    try {
      const data = await fetchJsonTimeout('/api/radar/sync', {method:'POST'}, 15000);
      if (status) status.textContent = data?.message || 'Команда принята. Радар запущен.';
      if (btn) btn.textContent = 'ПОИСК ИДЁТ…';
      await loadRadarLogs();
      setTimeout(() => {
        if (typeof refreshEverything === 'function') refreshEverything();
      }, 250);
    } catch (e) {
      if (status) status.textContent = `Не удалось запустить: ${e.message}`;
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'ЗАПУСТИТЬ ПОИСК';
      }
      await loadRadarLogs();
    }
  }

  const startButton = $local('#syncRadar');
  if (startButton) {
    // Capture phase prevents the legacy launcher from firing too.
    startButton.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      robustStartRadar();
    }, true);
  }

  const copyLogs = $local('#copyRadarLogs');
  if (copyLogs) {
    copyLogs.addEventListener('click', async () => {
      const rows = await fetchJsonTimeout('/api/radar/logs?limit=200', {}, 8000);
      const text = (rows || []).map(row => `[${row.created_at}] [${row.level}] [${row.stage}] ${row.message}${row.details ? ' | ' + JSON.stringify(row.details) : ''}`).join('\n');
      await copyText(text);
      const old = copyLogs.textContent;
      copyLogs.textContent = 'СКОПИРОВАНО';
      setTimeout(() => { copyLogs.textContent = old; }, 1200);
    });
  }

  loadRadarLogs();
  setInterval(() => { if (!document.hidden) loadRadarLogs(); }, 2500);
})();
