const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const copyRegistry = new Map();
let copyCounter = 0;
let refreshBundleInFlight = false;

const RETRYABLE_HTTP = new Set([408, 500, 502, 503, 504]);

class ApiError extends Error {
  constructor(message, {status = 0, transient = false} = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.transient = transient;
  }
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function escapeHtml(v = '') {
  return String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}
function escapeAttr(v = '') { return escapeHtml(v).replace(/\n/g, '&#10;'); }
function formatNum(v) {
  const n = Number(v || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + ' млн';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + ' тыс';
  return Math.round(n).toLocaleString('ru-RU');
}
function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return 'время не рассчитано';
  const s = Math.max(0, Number(seconds || 0));
  if (s <= 5) return 'меньше минуты';
  if (s < 60) return `≈ ${Math.ceil(s)} сек`;
  return `≈ ${Math.ceil(s / 60)} мин`;
}
function registerCopy(text) {
  const id = `copy-${++copyCounter}`;
  copyRegistry.set(id, String(text || ''));
  return id;
}
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}

function isTransientError(error) {
  if (Number(error?.status || 0) === 429) return false;
  return Boolean(error?.transient || error instanceof TypeError || RETRYABLE_HTTP.has(Number(error?.status || 0)));
}

function markReconnect() {
  const el = $('#serviceStatus');
  if (!el) return;
  el.classList.add('warn');
  el.innerHTML = '<i></i> Render перезапускается · переподключаюсь…';
}

async function apiJson(url, options = {}, config = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const retries = Number(config.retries ?? (method === 'GET' ? 2 : 0));
  const baseDelay = Number(config.baseDelay ?? 650);
  let lastError;

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        ...options,
        headers: {
          Accept: 'application/json',
          ...(options.headers || {}),
        },
      });

      const contentType = String(response.headers.get('content-type') || '').toLowerCase();
      const text = await response.text();
      const looksLikeHtml = contentType.includes('text/html') || /^\s*<!doctype/i.test(text) || /^\s*<html/i.test(text);

      if (looksLikeHtml) {
        throw new ApiError(
          'Render временно перезапускает сервис. Данные сохранены, переподключаюсь автоматически.',
          {status: response.status, transient: true}
        );
      }

      let data = null;
      if (text.trim()) {
        try {
          data = JSON.parse(text);
        } catch (_) {
          throw new ApiError('Сервер вернул некорректный ответ. Повторяю подключение…', {
            status: response.status,
            transient: true,
          });
        }
      }

      if (!response.ok) {
        const message = data?.error || data?.message || `Ошибка сервера ${response.status}`;
        throw new ApiError(message, {
          status: response.status,
          transient: RETRYABLE_HTTP.has(response.status),
        });
      }
      return data;
    } catch (error) {
      lastError = error instanceof ApiError
        ? error
        : new ApiError('Сервис временно недоступен. Переподключаюсь…', {transient: true});

      if (attempt >= retries || !isTransientError(lastError)) break;
      markReconnect();
      await sleep(baseDelay * Math.pow(1.7, attempt));
    }
  }
  throw lastError || new ApiError('Не удалось получить ответ сервера');
}

async function loadStatus() {
  const el = $('#serviceStatus');
  try {
    const s = await apiJson('/api/status');
    const ready = s.gemini_configured && s.apify_configured;
    el.innerHTML = `<i></i>${ready ? `готово · ${escapeHtml(s.analysis_model || '')}` : 'нужны API ключи'}`;
    el.classList.toggle('warn', !ready);
  } catch (e) {
    if (isTransientError(e)) markReconnect();
    else el.textContent = 'статус недоступен';
  }
}

function paintApiCard(name, data) {
  const card = $(`#${name}ApiCard`);
  const status = $(`#${name}ApiStatus`);
  const meta = $(`#${name}ApiMeta`);
  if (!card || !status || !meta) return;
  card.classList.remove('api-ok', 'api-bad', 'api-loading');
  if (!data) {
    card.classList.add('api-loading');
    status.textContent = 'Проверяю ключ…';
    meta.textContent = 'Минимальный сетевой тест';
    return;
  }
  if (data.transient) {
    card.classList.add('api-loading');
    status.textContent = 'Сервис перезапускается…';
    meta.textContent = data.label || 'Повторю проверку автоматически';
    return;
  }
  card.classList.add(data.ok ? 'api-ok' : 'api-bad');
  status.textContent = data.ok ? '✓ Ключ работает' : '✕ Нет подключения';
  const extra = [];
  if (data.model) extra.push(data.model);
  if (data.account) extra.push(data.account);
  if (data.latency_ms) extra.push(`${data.latency_ms} мс`);
  if (!data.ok && data.label) extra.push(data.label);
  meta.textContent = extra.join(' · ') || data.label || '';
}

async function checkApis() {
  const btn = $('#checkApis');
  if (btn) { btn.disabled = true; btn.textContent = 'ПРОВЕРЯЮ…'; }
  paintApiCard('gemini', null);
  paintApiCard('apify', null);
  try {
    const d = await apiJson('/api/diagnostics', {}, {retries: 3});
    paintApiCard('gemini', d.gemini);
    paintApiCard('apify', d.apify);
  } catch (e) {
    if (isTransientError(e)) {
      markReconnect();
      paintApiCard('gemini', {transient:true,label:'Render перезапускается — ключ не считается ошибочным'});
      paintApiCard('apify', {transient:true,label:'Render перезапускается — ключ не считается ошибочным'});
      setTimeout(() => checkApis(), 2500);
    } else {
      paintApiCard('gemini', {ok:false,label:e.message});
      paintApiCard('apify', {ok:false,label:e.message});
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'ПРОВЕРИТЬ API'; }
  }
}

async function loadRadarStatus() {
  try {
    const s = await apiJson('/api/radar/status');
    const pct = Math.max(0, Math.min(100, Number(s.progress || 0)));
    $('#radarProgressPct').textContent = `${pct}%`;
    $('#radarStage').textContent = s.label || 'Радар';
    $('#radarEta').textContent = s.stage === 'done' ? 'ГОТОВО' : (s.stage === 'error' ? 'ОШИБКА' : formatEta(s.eta_seconds));
    $('#radarProgressBar').style.width = `${pct}%`;
    $('#radarStatusMessage').textContent = s.message || '';

    const warning = $('#radarWarning');
    if (s.warning) {
      warning.textContent = s.warning;
      warning.classList.remove('hidden');
    } else {
      warning.classList.add('hidden');
      warning.textContent = '';
    }

    const d = s.details || {};
    const stats = [];
    if (d.raw !== undefined) stats.push(`<div><b>${formatNum(d.raw)}</b><span>сырых</span></div>`);
    if (d.numeric_candidates !== undefined) stats.push(`<div><b>${formatNum(d.numeric_candidates)}</b><span>≤10 сек / 7 дней</span></div>`);
    if (d.ai_total !== undefined) stats.push(`<div><b>${formatNum(d.ai_done || 0)}/${formatNum(d.ai_total)}</b><span>AI проверка</span></div>`);
    if (d.quality_top !== undefined) stats.push(`<div><b>${formatNum(d.quality_top)}</b><span>в сильном TOP</span></div>`);
    else if (d.matched !== undefined) stats.push(`<div><b>${formatNum(d.matched)}</b><span>AI совпадений</span></div>`);
    $('#radarPipelineStats').innerHTML = stats.join('');

    const syncBtn = $('#syncRadar');
    if (syncBtn) {
      const running = s.stage === 'running';
      syncBtn.disabled = running;
      syncBtn.textContent = running ? 'ПОИСК ИДЁТ…' : 'ЗАПУСТИТЬ ПОИСК';
    }
  } catch (e) {
    if (isTransientError(e)) markReconnect();
  }
}

function radarCard(x, i) {
  const anomaly = Number(x.anomaly_multiplier || 0);
  const usual = Number(x.creator_usual_views || 0);
  const followers = Number(x.followers_count || 0);
  const likes = Number(x.likes || 0);
  const comments = Number(x.comments || 0);
  const level = escapeHtml(x.priority_level || 'C');
  const label = escapeHtml(x.priority_label || 'НИЗКИЙ ПРИОРИТЕТ');
  const reason = escapeHtml(x.priority_reason || '');
  const anomalyHtml = usual > 0
    ? `<div class="anomaly ${anomaly >= 5 ? 'hot' : ''}">обычно ${formatNum(usual)} → <b>×${anomaly.toFixed(anomaly >= 10 ? 0 : 1)}</b></div>`
    : `<div class="anomaly muted-line">база автора собирается</div>`;
  const followerHtml = followers > 0 ? ` · ${formatNum(followers)} подписчиков` : '';
  return `<div class="radar-card priority-${level.toLowerCase()}">
    <div class="radar-rank">#${i + 1}</div>
    <div class="radar-score"><b>${Number(x.viral_score_v2 || 0).toFixed(0)}</b><small>VIRAL</small></div>
    <div class="radar-main">
      <div class="priority-badge priority-${level.toLowerCase()}">${label}</div>
      <div class="radar-title">${escapeHtml(x.hook || x.scene_description || 'AI-комедийная сценка')}</div>
      <div class="radar-meta">@${escapeHtml(x.creator)}${followerHtml} · ${Number(x.duration_sec || 0).toFixed(2)} сек · ${Number(x.hours_since_publish || 0).toFixed(1)} ч назад</div>
      <div class="radar-desc">${escapeHtml(x.scene_description || '')}</div>
      <div class="priority-reason">${reason}</div>${anomalyHtml}
    </div>
    <div class="radar-number"><b>${formatNum(x.views)}</b><small>просмотров · ${formatNum(likes)} лайков${comments ? ` · ${formatNum(comments)} комм.` : ''}</small></div>
    <div class="radar-number"><b class="accent">${formatNum(x.views_per_hour)}/ч</b><small>скорость</small></div>
    <div class="radar-actions">
      <a href="${escapeAttr(x.post_url)}" target="_blank" rel="noopener">ОРИГИНАЛ</a>
      <button data-radar-analyze="${x.id}">ПОЛУЧИТЬ УЛЬТРА-ПРОМПТЫ</button>
    </div>
  </div>`;
}

async function loadRadar() {
  const host = $('#radarRows');
  if (!host) return;
  try {
    const rows = await apiJson('/api/radar');
    if (!Array.isArray(rows) || !rows.length) {
      if (!host.querySelector('.radar-card')) {
        host.innerHTML = '<div class="empty">TOP пока формируется. Ниже уже видны кандидаты, полученные от Apify.</div>';
      }
      return;
    }
    host.innerHTML = rows.map((x, i) => radarCard(x, i)).join('');
    $$('[data-radar-analyze]', host).forEach(btn => btn.addEventListener('click', () => analyzeRadar(btn.dataset.radarAnalyze, btn)));
  } catch (e) {
    if (isTransientError(e)) {
      markReconnect();
      return;
    }
    if (!host.querySelector('.radar-card')) host.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function loadCandidates() {
  const host = $('#candidateRows');
  if (!host) return;
  try {
    const rows = await apiJson('/api/radar/candidates');
    if (!Array.isArray(rows) || !rows.length) {
      if (!host.querySelector('.candidate-row')) {
        host.innerHTML = '<div class="empty">Apify ещё не передал ни одного Reel, прошедшего объективный фильтр 7 дней + ≤10 секунд.</div>';
      }
      return;
    }
    host.innerHTML = rows.map((x, i) => {
      const state = x.ai_match ? 'ПРОШЁЛ AI' : (x.ai_checked ? 'НЕ ПРОШЁЛ AI' : 'ЖДЁТ AI');
      return `<div class="candidate-row ${x.ai_match ? 'candidate-ok' : ''}">
        <div><b>#${i+1} @${escapeHtml(x.creator)}</b><span>${Number(x.duration_sec || 0).toFixed(2)} сек · ${Number(x.hours_since_publish || 0).toFixed(1)} ч назад · ${escapeHtml(x.search_term || '')}</span></div>
        <div><b>${formatNum(x.views)}</b><span>${formatNum(x.likes)} лайков · ${formatNum(x.views_per_hour)}/ч</span></div>
        <div><b>${Number(x.viral_score_v2 || 0).toFixed(0)}</b><span>Viral</span></div>
        <div><span class="candidate-state">${state}</span></div>
        <a href="${escapeAttr(x.post_url)}" target="_blank" rel="noopener">ОТКРЫТЬ</a>
      </div>`;
    }).join('');
  } catch (e) {
    if (isTransientError(e)) {
      markReconnect();
      return;
    }
    if (!host.querySelector('.candidate-row')) host.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function loadRadarMeta() {
  const host = $('#radarMeta');
  if (!host) return;
  try {
    const data = await apiJson('/api/radar/meta');
    if (!data?.report) {
      if (!host.querySelector('.meta-card')) {
        host.innerHTML = '<div class="card"><div class="empty">Мета недели появится после первого завершённого поиска.</div></div>';
      }
      return;
    }
    const m = data.report;
    const clusters = (m.clusters || []).slice(0, 8).map(c => `<div class="meta-cluster"><div><b>${escapeHtml(c.label)}</b><span>${Number(c.reels_count || 0)} роликов</span></div><p>${escapeHtml(c.description || '')}</p></div>`).join('');
    const takeaways = (m.key_takeaways || []).map(x => `<li>${escapeHtml(x)}</li>`).join('');
    host.innerHTML = `<div class="card meta-card">
      <div class="card-head"><div><span class="step">МЕТА НЕДЕЛИ</span><h2>Что повторяется в TOP-${Number(data.source_count || 0)}</h2></div><span class="pill">средняя длина ${Number(data.average_duration_sec || 0).toFixed(2)} сек</span></div>
      <p class="meta-summary">${escapeHtml(m.summary || '')}</p>
      <div class="meta-stats"><div><span>Вопрос в хуке</span><b>${Number(m.question_hook_count || 0)} роликов</b></div><div><span>Персонажи</span><b>${escapeHtml((m.recurring_characters || []).slice(0, 3).join(' · ') || '—')}</b></div><div><span>Локации</span><b>${escapeHtml((m.recurring_settings || []).slice(0, 3).join(' · ') || '—')}</b></div></div>
      <div class="meta-clusters">${clusters}</div><div class="meta-takeaways"><b>Что брать в работу</b><ul>${takeaways}</ul></div>
    </div>`;
  } catch (e) {
    if (isTransientError(e)) {
      markReconnect();
      return;
    }
    if (!host.querySelector('.meta-card')) host.innerHTML = `<div class="card"><div class="error">${escapeHtml(e.message)}</div></div>`;
  }
}

async function refreshEverything() {
  if (refreshBundleInFlight) return;
  refreshBundleInFlight = true;
  try {
    await Promise.all([loadRadarStatus(), loadRadar(), loadCandidates(), loadRadarMeta(), loadStatus()]);
  } finally {
    refreshBundleInFlight = false;
  }
}

async function syncRadar() {
  const btn = $('#syncRadar');
  if (btn) { btn.disabled = true; btn.textContent = 'ЗАПУСКАЮ…'; }
  try {
    await apiJson('/health', {}, {retries: 4, baseDelay: 800});
    const d = await apiJson('/api/radar/sync', {method:'POST'}, {retries:0});
    $('#radarStatusMessage').textContent = d?.message || 'Радар запущен в фоне. Статус и результаты обновляются автоматически.';
    await refreshEverything();
  } catch (e) {
    if (isTransientError(e)) {
      markReconnect();
      $('#radarStatusMessage').textContent = 'Render сейчас перезапускается. Дождись зелёного статуса «готово» и нажми поиск ещё раз — данные не потеряны.';
    } else {
      $('#radarStatusMessage').textContent = e.message;
    }
    if (btn) { btn.disabled = false; btn.textContent = 'ЗАПУСТИТЬ ПОИСК'; }
  }
}

async function analyzeRadar(id, button = null) {
  const progress = $('#analysisProgress');
  const result = $('#analysisResult');
  const old = button?.textContent;
  if (button) { button.disabled = true; button.textContent = 'GEMINI АНАЛИЗИРУЕТ…'; }
  progress.classList.remove('hidden');
  result.classList.add('hidden');
  result.innerHTML = '';
  window.scrollTo({top: progress.offsetTop - 20, behavior:'smooth'});
  try {
    await apiJson('/health', {}, {retries: 4, baseDelay: 800});
    const data = await apiJson(`/api/radar/${id}/analyze`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({owned_or_licensed:false})
    }, {retries:0});
    renderResult(data.result, data.model || '');
  } catch (e) {
    const message = isTransientError(e)
      ? 'Render перезапустился во время запроса. Исходный Reel не потерян — после зелёного статуса нажми «Получить ультра-промпты» ещё раз.'
      : e.message;
    result.innerHTML = `<div class="error">${escapeHtml(message)}</div>`;
    result.classList.remove('hidden');
  } finally {
    progress.classList.add('hidden');
    if (button) { button.disabled = false; button.textContent = old || 'ПОЛУЧИТЬ УЛЬТРА-ПРОМПТЫ'; }
  }
}

function renderResult(d, modelName = '') {
  const result = $('#analysisResult');
  const b0 = d.block_0_director || {};
  const b2 = d.block_2_compliance || {};
  const b3 = d.block_3_video || {};
  const b4 = d.block_4_audio || {};
  const b5 = d.block_5_publication || {};
  const photoPrompt = d.block_1_frame0_prompt || '';
  const videoPrompt = JSON.stringify(b3, null, 2);
  const all = buildWholePackage(d);
  const photoId = registerCopy(photoPrompt);
  const videoId = registerCopy(videoPrompt);
  const allId = registerCopy(all);

  result.innerHTML = `
    <div class="result-top">
      <div class="stat"><span>Исходная длина</span><b>${Number(d.source_duration_sec || 0).toFixed(2)} сек</b></div>
      <div class="stat"><span>Итоговая длина</span><b class="accent">${Number(b3.exact_duration_sec || d.source_duration_sec || 0).toFixed(2)} сек</b></div>
      <div class="stat"><span>Язык</span><b>${escapeHtml(d.source_language || 'русский')}</b></div>
      <div class="stat"><span>QA точность</span><b>${Number(d.reconstruction_confidence || 0)}%</b></div>
    </div>
    <div class="card master-copy">
      <div><span class="step">ГОТОВО</span><h2>Frame 0 + ультра-детальный Video Prompt</h2><p>${escapeHtml(modelName || b3.model || 'Gemini')} · сначала генерируй стартовый кадр, затем оживляй его Video Prompt.</p></div>
      <div class="prompt-actions">
        <button class="copy-primary" data-copy-id="${photoId}">КОПИРОВАТЬ PHOTO PROMPT</button>
        <button class="copy-primary" data-copy-id="${videoId}">КОПИРОВАТЬ VIDEO PROMPT</button>
        <button class="copy-all" data-copy-id="${allId}">КОПИРОВАТЬ ВСЁ</button>
      </div>
    </div>
    ${blockCard('БЛОК 0','Режиссёрское решение',directorText(b0))}
    ${blockCard('БЛОК 1','PHOTO PROMPT — стартовый кадр',photoPrompt)}
    ${blockCard('БЛОК 2','Проверка Frame 0',complianceText(b2))}
    ${blockCard('БЛОК 3','VIDEO PROMPT — оживление',videoPrompt)}
    ${blockCard('БЛОК 4','Русская речь и аудио',audioText(b4))}
    ${blockCard('БЛОК 5','Публикация',publicationText(b5))}`;
  result.classList.remove('hidden');
  bindCopyButtons(result);
  window.scrollTo({top: result.offsetTop - 20, behavior:'smooth'});
}

function directorText(b){return [`Механика шутки\n${b.joke_mechanics||''}`,`Стратегия реконструкции\n${b.exact_copy_strategy||''}`,`Безопасные изменения\n${b.safety_adjustments||''}`,`Визуальная привязка\n${(b.visual_bindings||[]).join('\n')}`,`Локация\n${b.location||''}`,`Реализм\n${b.realism_decision||''}`].join('\n\n');}
function complianceText(b){return [`Персонажи\n${(b.characters||[]).join('\n')}`,`Позы и позиции\n${(b.poses_and_positions||[]).join('\n')}`,`Руки и предметы\n${(b.hands_and_objects||[]).join('\n')}`,`Отсутствие текста\n${b.no_text_check||''}`,`Первая фонема\n${b.first_phoneme_readiness||''}`,`Проверка реализма\n${(b.realism_check||[]).join('\n')}`].join('\n\n');}
function audioText(b){const d=(b.dialogue||[]).map(x=>`Линия ${x.line_number}\n${x.visual_speaker_binding}\n${x.text}`).join('\n\n');return [d,`Произношение\n${(b.pronunciation_hints||[]).join('\n')}`,`Интонации и смех\n${(b.intonation_and_laughter_map||[]).join('\n')}`].join('\n\n');}
function publicationText(b){return [`Пост\n${b.short_post||''}`,`Заголовок\n${b.shorts_title||''}`,`Фраза удержания\n${b.retention_phrase||''}`,`Хэштеги\n${(b.hashtags||[]).join(' ')}`].join('\n\n');}
function buildWholePackage(d){return [`БЛОК 0. РЕЖИССЁРСКОЕ РЕШЕНИЕ\n${directorText(d.block_0_director||{})}`,`БЛОК 1. PHOTO PROMPT — FRAME 0\n${d.block_1_frame0_prompt||''}`,`БЛОК 2. ПРОВЕРКА FRAME 0\n${complianceText(d.block_2_compliance||{})}`,`БЛОК 3. VIDEO PROMPT\n${JSON.stringify(d.block_3_video||{},null,2)}`,`БЛОК 4. РУССКАЯ РЕЧЬ И АУДИО\n${audioText(d.block_4_audio||{})}`,`БЛОК 5. ПУБЛИКАЦИЯ\n${publicationText(d.block_5_publication||{})}`].join('\n\n════════════════════════════════\n\n');}
function blockCard(tag,title,text){const id=registerCopy(text);return `<div class="card block-card"><div class="card-head"><div><span class="step">${tag}</span><h2>${escapeHtml(title)}</h2></div><button class="copy-mini" data-copy-id="${id}">КОПИРОВАТЬ</button></div><pre>${escapeHtml(text)}</pre></div>`;}
function bindCopyButtons(root=document){$$('[data-copy-id]',root).forEach(btn=>btn.addEventListener('click',async()=>{const text=copyRegistry.get(btn.dataset.copyId)||'';await copyText(text);const old=btn.textContent;btn.textContent='СКОПИРОВАНО';setTimeout(()=>btn.textContent=old,1200);}));}

$('#syncRadar')?.addEventListener('click', syncRadar);
$('#refreshRadar')?.addEventListener('click', refreshEverything);
$('#checkApis')?.addEventListener('click', checkApis);

loadStatus();
checkApis();
refreshEverything();
setInterval(() => { if (!document.hidden) loadRadarStatus(); }, 4000);
setInterval(() => { if (!document.hidden) { loadRadar(); loadCandidates(); } }, 10000);
setInterval(() => { if (!document.hidden) loadRadarMeta(); }, 30000);
window.addEventListener('online', () => refreshEverything());
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshEverything(); });