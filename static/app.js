const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];

$$('.nav').forEach(btn => btn.addEventListener('click', () => {
  $$('.nav').forEach(x => x.classList.remove('active'));
  $$('.screen').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  $('#' + btn.dataset.target).classList.add('active');
  if(btn.dataset.target === 'radar') loadRadar();
}));

const videoInput = $('#video');
const drop = $('#dropzone');
['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', e => { if (e.dataTransfer.files?.[0]) { videoInput.files = e.dataTransfer.files; showFile(); } });
videoInput.addEventListener('change', showFile);
function showFile(){ const f=videoInput.files?.[0]; if(!f)return; $('#dropTitle').textContent=f.name; $('#dropSub').textContent=`${(f.size/1024/1024).toFixed(1)} МБ`; }

$('#analyzeForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = new FormData(e.currentTarget);
  form.set('owned_or_licensed', $('#rights').checked ? 'true' : 'false');
  await runAnalysis(() => fetch('/api/analyze', { method:'POST', body:form }));
});

async function runAnalysis(requestFn){
  const btn = $('#analyzeBtn');
  if(btn) btn.disabled = true;
  $('#progress').classList.remove('hidden');
  $('#result').classList.add('hidden');
  $('#result').innerHTML = '';
  try {
    const r = await requestFn();
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || 'Ошибка анализа');
    renderResult(data);
  } catch(err) {
    $('#result').innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
    $('#result').classList.remove('hidden');
  } finally {
    if(btn) btn.disabled = false;
    $('#progress').classList.add('hidden');
  }
}

$$('.library-card').forEach(card => card.addEventListener('click', async () => {
  const r = await fetch(`/api/analysis/${card.dataset.id}`);
  const data = await r.json();
  if(r.ok){ showAnalyzer(); renderResult({result:data.result}); window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'}); }
}));

function showAnalyzer(){
  $$('.nav').forEach(x => x.classList.remove('active'));
  $$('.screen').forEach(x => x.classList.remove('active'));
  $('[data-target="analyzer"]').classList.add('active');
  $('#analyzer').classList.add('active');
}

function renderResult(payload){
  const d = payload.result;
  const b0 = d.block_0_director || {};
  const b2 = d.block_2_compliance || {};
  const b3 = d.block_3_video || {};
  const b4 = d.block_4_audio || {};
  const b5 = d.block_5_publication || {};

  const allPackage = buildWholePackage(d);
  const html = `
    <div class="result-top">
      <div class="stat"><span>Длительность</span><b>${escapeHtml(d.source_duration_sec ?? 0)} сек</b></div>
      <div class="stat"><span>Язык</span><b>${escapeHtml(d.source_language || 'русский')}</b></div>
      <div class="stat"><span>Режим</span><b class="accent">1:1</b></div>
      <div class="stat"><span>Точность разбора</span><b>${escapeHtml(d.reconstruction_confidence ?? 0)}%</b></div>
    </div>
    <div class="card master-copy"><div><span class="step">ГОТОВО</span><h2>Полный пакет 0–5</h2><p>Можно копировать целиком или по отдельным блокам.</p></div><button class="copy-all" data-copy="${escapeAttr(allPackage)}">КОПИРОВАТЬ ВСЁ</button></div>
    ${blockCard('БЛОК 0', 'Режиссёрское решение', directorText(b0))}
    ${blockCard('БЛОК 1', 'Первый кадр', d.block_1_frame0_prompt || '')}
    ${blockCard('БЛОК 2', 'Карта проверки первого кадра', complianceText(b2))}
    ${blockCard('БЛОК 3', 'Промпт для видео', JSON.stringify(b3, null, 2))}
    ${blockCard('БЛОК 4', 'Аудио', audioText(b4))}
    ${blockCard('БЛОК 5', 'Публикация', publicationText(b5))}
  `;
  $('#result').innerHTML = html;
  $('#result').classList.remove('hidden');
  bindCopyButtons($('#result'));
}

function directorText(b){
  return [
    `Механика шутки\n${b.joke_mechanics||''}`,
    `Стратегия копирования\n${b.exact_copy_strategy||''}`,
    `Безопасные изменения\n${b.safety_adjustments||''}`,
    `Визуальная привязка\n${(b.visual_bindings||[]).join('\n')}`,
    `Локация\n${b.location||''}`,
    `Бюджет смеха\n${b.laughter_budget||''}`,
    `Реализм\n${b.realism_decision||''}`
  ].join('\n\n');
}
function complianceText(b){
  return [
    `Персонажи\n${(b.characters||[]).join('\n')}`,
    `Позы и позиции\n${(b.poses_and_positions||[]).join('\n')}`,
    `Руки и предметы\n${(b.hands_and_objects||[]).join('\n')}`,
    `Отсутствие текста\n${b.no_text_check||''}`,
    `Первая фонема\n${b.first_phoneme_readiness||''}`,
    `Проверка реализма\n${(b.realism_check||[]).join('\n')}`
  ].join('\n\n');
}
function audioText(b){
  const dialogue=(b.dialogue||[]).map(x=>`Линия ${x.line_number}\n${x.visual_speaker_binding}\n${x.text}`).join('\n\n');
  return [dialogue, `Произношение\n${(b.pronunciation_hints||[]).join('\n')}`, `Интонации и смех\n${(b.intonation_and_laughter_map||[]).join('\n')}`].join('\n\n');
}
function publicationText(b){
  return [`Пост\n${b.short_post||''}`,`Заголовок\n${b.shorts_title||''}`,`Фраза удержания\n${b.retention_phrase||''}`,`Хэштеги\n${(b.hashtags||[]).join(' ')}`].join('\n\n');
}
function buildWholePackage(d){
  return [
    `БЛОК 0. РЕЖИССЁРСКОЕ РЕШЕНИЕ\n${directorText(d.block_0_director||{})}`,
    `БЛОК 1. ПЕРВЫЙ КАДР\n${d.block_1_frame0_prompt||''}`,
    `БЛОК 2. КАРТА ПРОВЕРКИ\n${complianceText(d.block_2_compliance||{})}`,
    `БЛОК 3. ВИДЕО\n${JSON.stringify(d.block_3_video||{},null,2)}`,
    `БЛОК 4. АУДИО\n${audioText(d.block_4_audio||{})}`,
    `БЛОК 5. ПУБЛИКАЦИЯ\n${publicationText(d.block_5_publication||{})}`,
  ].join('\n\n════════════════════════════════\n\n');
}
function blockCard(tag,title,text){
  return `<div class="card"><div class="card-head"><div><span class="step">${tag}</span><h2>${title}</h2></div></div><div class="prompt-box" data-prompt="${escapeAttr(text)}"><button class="copy">КОПИРОВАТЬ</button>${escapeHtml(text)}</div></div>`;
}
function bindCopyButtons(root){
  $$('.copy', root).forEach(b => b.addEventListener('click', async ()=>{
    await navigator.clipboard.writeText(b.parentElement.dataset.prompt || '');
    const old=b.textContent; b.textContent='СКОПИРОВАНО'; setTimeout(()=>b.textContent=old,1200);
  }));
  $$('.copy-all', root).forEach(b=>b.addEventListener('click',async()=>{
    await navigator.clipboard.writeText(b.dataset.copy||''); const old=b.textContent; b.textContent='СКОПИРОВАНО'; setTimeout(()=>b.textContent=old,1200);
  }));
}

async function loadRadar(){
  const host=$('#radarRows');
  if(!host)return;
  host.innerHTML='<div class="empty">Загружаю радар…</div>';
  await loadRadarMeta();
  try{
    const r=await fetch('/api/radar');
    const rows=await r.json();
    if(!r.ok) throw new Error('Не удалось загрузить радар');
    if(!rows.length){
      host.innerHTML='<div class="empty">Пока нет отобранных роликов. Нужен первый запуск радара.</div>';
      return;
    }
    host.innerHTML=rows.map((x,i)=>{
      const hot = x.views >= 100000 && x.hours_since_publish <= 72 ? '🔥' : '';
      const anomaly = Number(x.anomaly_multiplier||0);
      const usual = Number(x.creator_usual_views||0);
      const followers = Number(x.followers_count||0);
      const anomalyHtml = usual > 0
        ? `<div class="anomaly ${anomaly>=5?'hot':''}">обычно ${formatNum(usual)} → <b>×${anomaly.toFixed(anomaly>=10?0:1)}</b></div>`
        : `<div class="anomaly muted-line">база автора собирается</div>`;
      const followerHtml = followers > 0 ? ` · ${formatNum(followers)} подписчиков` : '';
      return `<div class="radar-card">
        <div class="radar-rank">#${i+1}</div>
        <div class="radar-score"><b>${Number(x.viral_score_v2||0).toFixed(0)}</b><small>VIRAL</small></div>
        <div class="radar-main">
          <div class="radar-title">${hot} ${escapeHtml(x.hook||x.scene_description||'Подходящая AI-сценка')}</div>
          <div class="radar-meta">@${escapeHtml(x.creator)}${followerHtml} · ${Number(x.duration_sec||0).toFixed(1)} сек · ${Number(x.hours_since_publish||0).toFixed(1)} ч назад</div>
          <div class="radar-desc">${escapeHtml(x.scene_description||'')}</div>
          ${anomalyHtml}
        </div>
        <div class="radar-number"><b>${formatNum(x.views)}</b><small>просмотров</small></div>
        <div class="radar-number"><b class="accent">${formatNum(x.views_per_hour)}/ч</b><small>скорость</small></div>
        <div class="radar-actions"><a href="${escapeAttr(x.post_url)}" target="_blank" rel="noopener">ОТКРЫТЬ</a><button data-radar-analyze="${x.id}">РАЗОБРАТЬ 1:1</button></div>
      </div>`;
    }).join('');
    $$('[data-radar-analyze]',host).forEach(btn=>btn.addEventListener('click',()=>analyzeRadar(btn.dataset.radarAnalyze)));
  }catch(e){
    host.innerHTML=`<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function loadRadarMeta(){
  const host=$('#radarMeta');
  if(!host)return;
  try{
    const r=await fetch('/api/radar/meta');
    const data=await r.json();
    if(!r.ok) throw new Error('Не удалось загрузить мету недели');
    if(!data?.report){
      host.innerHTML='<div class="card"><div class="empty">Мета недели появится после первого полного поиска и анализа TOP.</div></div>';
      return;
    }
    const m=data.report;
    const clusters=(m.clusters||[]).slice(0,8).map(c=>`
      <div class="meta-cluster">
        <div><b>${escapeHtml(c.label)}</b><span>${Number(c.reels_count||0)} роликов</span></div>
        <p>${escapeHtml(c.description||'')}</p>
      </div>`).join('');
    const takeaways=(m.key_takeaways||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
    host.innerHTML=`
      <div class="card meta-card">
        <div class="card-head">
          <div><span class="step">МЕТА НЕДЕЛИ</span><h2>Что прямо сейчас повторяется в TOP-${Number(data.source_count||0)}</h2></div>
          <span class="pill">средняя длина ${Number(data.average_duration_sec||0).toFixed(1)} сек</span>
        </div>
        <p class="meta-summary">${escapeHtml(m.summary||'')}</p>
        <div class="meta-stats">
          <div><span>Вопрос в хуке</span><b>${Number(m.question_hook_count||0)} роликов</b></div>
          <div><span>Персонажи</span><b>${escapeHtml((m.recurring_characters||[]).slice(0,3).join(' · ')||'—')}</b></div>
          <div><span>Локации</span><b>${escapeHtml((m.recurring_settings||[]).slice(0,3).join(' · ')||'—')}</b></div>
        </div>
        <div class="meta-clusters">${clusters}</div>
        <div class="meta-takeaways"><b>Что брать в работу</b><ul>${takeaways}</ul></div>
      </div>`;
  }catch(e){
    host.innerHTML=`<div class="card"><div class="error">${escapeHtml(e.message)}</div></div>`;
  }
}

async function analyzeRadar(id){
  showAnalyzer();
  await runAnalysis(()=>fetch(`/api/radar/${id}/analyze`,{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({owned_or_licensed:$('#rights')?.checked||false})
  }));
}
$('#refreshRadar')?.addEventListener('click',()=>loadRadar());

function formatNum(v){
  const n=Number(v||0);
  if(n>=1e6)return (n/1e6).toFixed(n>=1e7?0:1)+' млн';
  if(n>=1e3)return (n/1e3).toFixed(n>=1e5?0:1)+' тыс';
  return Math.round(n).toLocaleString('ru-RU');
}
function escapeHtml(v=''){return String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function escapeAttr(v=''){return escapeHtml(v).replace(/\n/g,'&#10;');}

async function loadStatus(){
  const el=$('#serviceStatus');
  if(!el)return;
  try{
    const r=await fetch('/api/status');
    const s=await r.json();
    if(!r.ok) throw new Error('status');
    const ready=s.gemini_configured && s.apify_configured;
    el.innerHTML=`<i></i>${ready?'API подключены':'нужны 2 API ключа'}`;
    if(!ready) el.classList.add('warn'); else el.classList.remove('warn');
  }catch(_){
    el.textContent='статус недоступен';
  }
}

async function syncRadar(){
  const out=$('#radarSyncResult');
  const btn=$('#syncRadar');
  if(btn){btn.disabled=true; btn.textContent='ИЩУ РОЛИКИ…';}
  if(out) out.textContent='Запущен поиск по запросам хештегам и отслеживаемым авторам. Затем считаю Viral Score 2.0 и мету TOP. Это может занять несколько минут.';
  try{
    const r=await fetch('/api/radar/sync',{method:'POST'});
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||'Ошибка синхронизации');
    if(out) out.textContent=`Готово: собрано ${d.raw||0}, после фильтра ${d.after_numeric_filter||0}, просмотрено моделью ${d.ai_checked||0}, подошло ${d.matched||0}, ошибок источников ${d.source_errors||0}.`;
    await loadRadar();
    await loadStatus();
  }catch(e){
    if(out) out.textContent=e.message;
  }finally{
    if(btn){btn.disabled=false; btn.textContent='ЗАПУСТИТЬ ПОИСК';}
  }
}
$('#syncRadar')?.addEventListener('click',syncRadar);

loadStatus();
