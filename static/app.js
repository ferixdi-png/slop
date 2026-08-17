const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

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

async function loadStatus() {
  const el = $('#serviceStatus');
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const ready = s.gemini_configured && s.apify_configured;
    el.innerHTML = `<i></i>${ready ? 'ключи заданы' : 'нужны API ключи'}`;
    el.classList.toggle('warn', !ready);
  } catch (_) {
    el.textContent = 'статус недоступен';
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
    const r = await fetch('/api/diagnostics', {cache: 'no-store'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Не удалось проверить API');
    paintApiCard('gemini', d.gemini);
    paintApiCard('apify', d.apify);
  } catch (e) {
    paintApiCard('gemini', {ok:false,label:e.message});
    paintApiCard('apify', {ok:false,label:e.message});
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'ПРОВЕРИТЬ API'; }
  }
}

async function loadRadarStatus() {
  try {
    const r = await fetch('/api/radar/status', {cache: 'no-store'});
    const s = await r.json();
    if (!r.ok) return;
    const pct = Math.max(0, Math.min(100, Number(s.progress || 0)));
    $('#radarProgressPct').textContent = `${pct}%`;
    $('#radarStage').textContent = s.label || 'Радар';
    $('#radarEta').textContent = s.stage === 'done' ? 'ГОТОВО' : formatEta(s.eta_seconds);
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
    if (d.matched !== undefined) stats.push(`<div><b>${formatNum(d.matched)}</b><span>подошло</span></div>`);
    $('#radarPipelineStats').innerHTML = stats.join('');

    const syncBtn = $('#syncRadar');
    if (syncBtn) {
      const running = s.stage === 'running';
      syncBtn.disabled = running;
      syncBtn.textContent = running ? 'ПОИСК ИДЁТ…' : 'ЗАПУСТИТЬ ПОИСК';
    }
  } catch (_) {}
}

async function loadRadar() {
  const host = $('#radarRows');
  if (!host) return;
  try {
    const r = await fetch('/api/radar', {cache: 'no-store'});
    const rows = await r.json();
    if (!r.ok) throw new Error(rows.error || 'Не удалось загрузить TOP');
    if (!rows.length) {
      host.innerHTML = '<div class="empty">Пока никто не прошёл финальный AI-фильтр. Ниже смотри кандидатов, уже полученных от Apify.</div>';
      return;
    }
    host.innerHTML = rows.map((x, i) => radarCard(x, i)).join('');
    $$('[data-radar-analyze]', host).forEach(btn => btn.addEventListener('click', () => analyzeRadar(btn.dataset.radarAnalyze)));
  } catch (e) {
    host.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

function radarCard(x, i) {
  const anomaly = Number(x.anomaly_multiplier || 0);
  const usual = Number(x.creator_usual_views || 0);
  const followers = Number(x.followers_count || 0);
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
    <div class="radar-number"><b>${formatNum(x.views)}</b><small>просмотров</small></div>
    <div class="radar-number"><b class="accent">${formatNum(x.views_per_hour)}/ч</b><small>скорость</small></div>
    <div class="radar-actions">
      <a href="${escapeAttr(x.post_url)}" target="_blank" rel="noopener">ОРИГИНАЛ</a>
      <button data-radar-analyze="${x.id}">СОБРАТЬ ПРОМПТЫ 1:1</button>
    </div>
  </div>`;
}

async function loadCandidates() {
  const host = $('#candidateRows');
  if (!host) return;
  try {
    const r = await fetch('/api/radar/candidates', {cache:'no-store'});
    const rows = await r.json();
    if (!r.ok) throw new Error('Не удалось загрузить кандидатов');
    if (!rows.length) {
      host.innerHTML = '<div class="empty">Apify ещё не передал ни одного ролика, прошедшего фильтр 7 дней + ≤10 секунд.</div>';
      return;
    }
    host.innerHTML = rows.map((x, i) => {
      const state = x.ai_match ? 'ПРОШЁЛ AI' : (x.ai_checked ? 'НЕ ПРОШЁЛ AI' : 'ЖДЁТ AI');
      return `<div class="candidate-row ${x.ai_match ? 'candidate-ok' : ''}">
        <div><b>#${i+1} @${escapeHtml(x.creator)}</b><span>${Number(x.duration_sec || 0).toFixed(2)} сек · ${Number(x.hours_since_publish || 0).toFixed(1)} ч назад · ${escapeHtml(x.search_term || '')}</span></div>
        <div><b>${formatNum(x.views)}</b><span>${formatNum(x.views_per_hour)}/ч</span></div>
        <div><b>${Number(x.viral_score_v2 || 0).toFixed(0)}</b><span>Viral</span></div>
        <div><span class="candidate-state">${state}</span></div>
        <a href="${escapeAttr(x.post_url)}" target="_blank" rel="noopener">ОТКРЫТЬ</a>
      </div>`;
    }).join('');
  } catch (e) {
    host.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function loadRadarMeta() {
  const host = $('#radarMeta');
  if (!host) return;
  try {
    const r = await fetch('/api/radar/meta', {cache:'no-store'});
    const data = await r.json();
    if (!r.ok) throw new Error('Не удалось загрузить мету недели');
    if (!data?.report) {
      host.innerHTML = '<div class="card"><div class="empty">Мета недели появится после первого завершённого поиска.</div></div>';
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
    host.innerHTML = `<div class="card"><div class="error">${escapeHtml(e.message)}</div></div>`;
  }
}

async function refreshEverything() {
  await Promise.all([loadRadarStatus(), loadRadar(), loadCandidates(), loadRadarMeta(), loadStatus()]);
}

async function syncRadar() {
  const btn = $('#syncRadar');
  if (btn) { btn.disabled = true; btn.textContent = 'ПОИСК ИДЁТ…'; }
  const poll = setInterval(refreshEverything, 5000);
  try {
    const r = await fetch('/api/radar/sync', {method:'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Ошибка синхронизации');
  } catch (e) {
    $('#radarStatusMessage').textContent = e.message;
  } finally {
    clearInterval(poll);
    await refreshEverything();
  }
}

async function analyzeRadar(id) {
  const progress = $('#analysisProgress');
  const result = $('#analysisResult');
  progress.classList.remove('hidden');
  result.classList.add('hidden');
  result.innerHTML = '';
  window.scrollTo({top: progress.offsetTop - 20, behavior:'smooth'});
  try {
    const r = await fetch(`/api/radar/${id}/analyze`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owned_or_licensed:false})});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Ошибка разбора');
    renderResult(data.result);
  } catch (e) {
    result.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
    result.classList.remove('hidden');
  } finally {
    progress.classList.add('hidden');
  }
}

function renderResult(d) {
  const result = $('#analysisResult');
  const b0 = d.block_0_director || {};
  const b2 = d.block_2_compliance || {};
  const b3 = d.block_3_video || {};
  const b4 = d.block_4_audio || {};
  const b5 = d.block_5_publication || {};
  const all = buildWholePackage(d);
  result.innerHTML = `<div class="result-top"><div class="stat"><span>Исходная длина</span><b>${Number(d.source_duration_sec || 0).toFixed(2)} сек</b></div><div class="stat"><span>Итоговая длина</span><b class="accent">${Number(b3.exact_duration_sec || d.source_duration_sec || 0).toFixed(2)} сек</b></div><div class="stat"><span>Язык</span><b>${escapeHtml(d.source_language || 'русский')}</b></div><div class="stat"><span>QA точность</span><b>${Number(d.reconstruction_confidence || 0)}%</b></div></div>
    <div class="card master-copy"><div><span class="step">ГОТОВО</span><h2>Frame 0 + production package</h2><p>Сначала стартовый кадр из Блока 1, затем оживление Блоком 3.</p></div><button class="copy-all" data-copy="${escapeAttr(all)}">КОПИРОВАТЬ ВСЁ</button></div>
    ${blockCard('БЛОК 0','Режиссёрское решение',directorText(b0))}${blockCard('БЛОК 1','PHOTO PROMPT — стартовый кадр',d.block_1_frame0_prompt || '')}${blockCard('БЛОК 2','Проверка Frame 0',complianceText(b2))}${blockCard('БЛОК 3','VIDEO PROMPT — оживление',JSON.stringify(b3,null,2))}${blockCard('БЛОК 4','Русская речь и аудио',audioText(b4))}${blockCard('БЛОК 5','Публикация',publicationText(b5))}`;
  result.classList.remove('hidden');
  bindCopyButtons(result);
  window.scrollTo({top: result.offsetTop - 20, behavior:'smooth'});
}
function directorText(b){return [`Механика шутки\n${b.joke_mechanics||''}`,`Стратегия реконструкции\n${b.exact_copy_strategy||''}`,`Безопасные изменения\n${b.safety_adjustments||''}`,`Визуальная привязка\n${(b.visual_bindings||[]).join('\n')}`,`Локация\n${b.location||''}`,`Реализм\n${b.realism_decision||''}`].join('\n\n');}
function complianceText(b){return [`Персонажи\n${(b.characters||[]).join('\n')}`,`Позы и позиции\n${(b.poses_and_positions||[]).join('\n')}`,`Руки и предметы\n${(b.hands_and_objects||[]).join('\n')}`,`Отсутствие текста\n${b.no_text_check||''}`,`Первая фонема\n${b.first_phoneme_readiness||''}`,`Проверка реализма\n${(b.realism_check||[]).join('\n')}`].join('\n\n');}
function audioText(b){const d=(b.dialogue||[]).map(x=>`Линия ${x.line_number}\n${x.visual_speaker_binding}\n${x.text}`).join('\n\n');return [d,`Произношение\n${(b.pronunciation_hints||[]).join('\n')}`,`Интонации и смех\n${(b.intonation_and_laughter_map||[]).join('\n')}`].join('\n\n');}
function publicationText(b){return [`Пост\n${b.short_post||''}`,`Заголовок\n${b.shorts_title||''}`,`Фраза удержания\n${b.retention_phrase||''}`,`Хэштеги\n${(b.hashtags||[]).join(' ')}`].join('\n\n');}
function buildWholePackage(d){return [`БЛОК 0. РЕЖИССЁРСКОЕ РЕШЕНИЕ\n${directorText(d.block_0_director||{})}`,`БЛОК 1. PHOTO PROMPT — FRAME 0\n${d.block_1_frame0_prompt||''}`,`БЛОК 2. ПРОВЕРКА FRAME 0\n${complianceText(d.block_2_compliance||{})}`,`БЛОК 3. VIDEO PROMPT\n${JSON.stringify(d.block_3_video||{},null,2)}`,`БЛОК 4. РУССКАЯ РЕЧЬ И АУДИО\n${audioText(d.block_4_audio||{})}`,`БЛОК 5. ПУБЛИКАЦИЯ\n${publicationText(d.block_5_publication||{})}`].join('\n\n════════════════════════════════\n\n');}
function blockCard(tag,title,text){return `<div class="card block-card"><div class="card-head"><div><span class="step">${tag}</span><h2>${escapeHtml(title)}</h2></div><button class="copy-mini" data-copy="${escapeAttr(text)}">КОПИРОВАТЬ</button></div><pre>${escapeHtml(text)}</pre></div>`;}
function bindCopyButtons(root=document){$$('[data-copy]',root).forEach(btn=>btn.addEventListener('click',async()=>{await navigator.clipboard.writeText(btn.dataset.copy||'');const old=btn.textContent;btn.textContent='СКОПИРОВАНО';setTimeout(()=>btn.textContent=old,1200);}));}

$('#syncRadar')?.addEventListener('click', syncRadar);
$('#refreshRadar')?.addEventListener('click', refreshEverything);
$('#checkApis')?.addEventListener('click', checkApis);

loadStatus();
checkApis();
refreshEverything();
setInterval(loadRadarStatus, 4000);
setInterval(() => { loadRadar(); loadCandidates(); }, 12000);
