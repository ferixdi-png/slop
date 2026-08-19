from __future__ import annotations

import hashlib

from flask import Response, jsonify, request


PROFILE = "frontend_v33_fail_open_single_runtime"

HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>ПОИСК ТРЕНДОВ — INSTAGRAM + TIKTOK + YOUTUBE</title>
<style>
:root{--bg:#080b10;--panel:#10151c;--panel2:#151b24;--line:#29313d;--text:#f4f7fb;--muted:#8d98a8;--accent:#b7ff33;--warn:#ffbd66;--bad:#ff6f78;--good:#74e39b}*{box-sizing:border-box}html{background:var(--bg);color:var(--text)}body{margin:0;min-height:100vh;background:radial-gradient(circle at 78% -12%,#1b2837 0,transparent 36%),var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.app-shell{min-height:100vh;display:grid;grid-template-columns:250px minmax(0,1fr)}aside{position:sticky;top:0;height:100vh;padding:26px 18px;border-right:1px solid var(--line);background:#0b0f15}.brand{display:flex;align-items:center;gap:12px}.brand-mark{width:40px;height:40px;display:grid;place-items:center;border-radius:11px;background:var(--accent);color:#091008;font-weight:950}.brand strong,.brand small{display:block}.brand strong{font-size:14px}.brand small{margin-top:3px;color:var(--muted);font-size:11px}.side-note{margin-top:26px;padding:14px;border:1px solid var(--line);border-radius:14px;color:#8e99a7;font-size:11px;line-height:1.6}.runtime-pill{margin-top:14px;padding:10px 12px;border:1px solid #4f5f35;border-radius:10px;background:#151d10;color:#dfffaa;font-size:11px;font-weight:800;line-height:1.45}.runtime-pill.warn{border-color:#72592f;background:#231b0e;color:#ffdba5}.runtime-pill.bad{border-color:#70343b;background:#261317;color:#ffd5d9}main{width:100%;max-width:1480px;margin:0 auto;padding:40px 48px 72px}header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:30px}.eyebrow{color:var(--accent);font-size:11px;font-weight:900;letter-spacing:.16em;margin-bottom:10px}h1{margin:0;font-size:clamp(34px,5vw,68px);line-height:.98;letter-spacing:-.05em}h1 span{color:#7e8998}.service{border:1px solid var(--line);border-radius:999px;padding:8px 12px;color:#a5afbc;font-size:11px;white-space:nowrap}.service.good{border-color:#31563e;color:#a8f0bf}.service.warn{border-color:#6c542f;color:#ffd18a}.runtime-error{margin:0 0 18px;padding:13px 15px;border:1px solid #743842;border-radius:12px;background:#281318;color:#ffd8dc;font-size:12px;line-height:1.5}.card{background:linear-gradient(180deg,#121820,#0f141a);border:1px solid var(--line);border-radius:17px;padding:20px}.section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin:22px 0 12px}.section-head h2{margin:0;font-size:18px}.section-head span{color:var(--muted);font-size:11px;line-height:1.5}.api-grid{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;margin-bottom:14px}.api-card strong,.api-card small{display:block}.api-card strong{font-size:14px}.api-card small{margin-top:7px;color:var(--muted);font-size:11px;line-height:1.45}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.btn{appearance:none;border:1px solid #48562f;border-radius:10px;background:#192412;color:var(--accent);padding:10px 13px;font-size:10px;font-weight:900;cursor:pointer}.btn:hover{filter:brightness(1.08)}.btn:disabled{opacity:.48;cursor:not-allowed}.btn.ghost{border-color:var(--line);background:#11171e;color:#b7c0cd}.btn.danger{border-color:#713b42;background:#271419;color:#ffadb3}.radar-intro{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:12px}.radar-intro b,.radar-intro span{display:block}.radar-intro b{font-size:14px}.radar-intro span{margin-top:6px;color:var(--muted);font-size:11px;line-height:1.55}.progress-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.progress-head b{font-size:18px}.progress-head small{display:block;margin-top:5px;color:var(--muted)}.pct{color:var(--accent);font-size:20px;font-weight:950}.track{height:8px;margin:15px 0 12px;border-radius:99px;background:#090d12;overflow:hidden;border:1px solid #202732}.fill{width:0;height:100%;background:var(--accent);transition:width .25s ease}.warning{margin-top:12px;padding:10px 12px;border:1px solid #6f542b;border-radius:10px;background:#231a0c;color:#ffd28b;font-size:11px}.stats{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.stat{min-width:110px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#0b1016}.stat b,.stat span{display:block}.stat b{font-size:16px}.stat span{margin-top:2px;color:var(--muted);font-size:9px;text-transform:uppercase}.meta{color:#aeb7c3;font-size:11px;line-height:1.55}.rows{display:grid;gap:8px}.radar-row{display:grid;grid-template-columns:52px 80px minmax(240px,1fr) 125px 125px 190px;gap:12px;align-items:center;padding:13px;border:1px solid var(--line);border-radius:12px;background:#0b1016}.rank{color:var(--accent);font-size:17px;font-weight:950}.score b,.metric b{display:block}.score b{font-size:20px}.score small,.metric small,.row-meta{color:var(--muted);font-size:9px}.row-title{font-size:13px;font-weight:850}.row-desc{margin-top:5px;color:#a5afbd;font-size:10px;line-height:1.45}.badge{display:inline-block;margin:0 5px 5px 0;padding:4px 6px;border:1px solid #40532c;border-radius:99px;background:#172110;color:#cfff84;font-size:8px;font-weight:900}.candidate{display:grid;grid-template-columns:minmax(220px,1.4fr) 170px 140px;gap:12px;align-items:center;padding:11px 12px;border-bottom:1px solid #202732}.candidate:last-child{border-bottom:0}.candidate b,.candidate span{display:block}.candidate b{font-size:11px}.candidate span{margin-top:3px;color:var(--muted);font-size:9px}.candidate-state{font-size:9px;font-weight:900;color:#d8dfe8}.candidate-state.ok{color:var(--good)}.candidate-state.no{color:#ff9ba2}.empty{padding:24px;border:1px dashed var(--line);border-radius:12px;color:var(--muted);font-size:11px}.analysis-loading{margin-bottom:12px;color:#d8e3ef;font-size:12px}.analysis-result{display:grid;gap:10px}.analysis-block{position:relative;padding:16px;border:1px solid var(--line);border-radius:13px;background:#0a0f15}.analysis-block h3{margin:0 0 10px;font-size:12px}.analysis-block pre{margin:0;white-space:pre-wrap;word-break:break-word;color:#d4dce7;font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.copy{position:absolute;right:10px;top:10px}.footer-note{margin-top:18px;color:#697585;font-size:10px;line-height:1.5}[hidden]{display:none!important}@media(max-width:1050px){.app-shell{display:block}aside{position:relative;height:auto;border-right:0;border-bottom:1px solid var(--line)}.side-note{display:none}main{padding:28px 18px 60px}.radar-row{grid-template-columns:42px 65px 1fr}.radar-row>.metric,.radar-row>.row-actions{grid-column:3}.api-grid{grid-template-columns:1fr 1fr}.api-grid>.btn{grid-column:1/3}.candidate{grid-template-columns:1fr 130px}}@media(max-width:700px){header,.radar-intro,.section-head{display:grid}.service{white-space:normal}.api-grid{grid-template-columns:1fr}.api-grid>.btn{grid-column:auto}.radar-row{grid-template-columns:38px 1fr}.radar-row>.score,.radar-row>.row-main,.radar-row>.metric,.radar-row>.row-actions{grid-column:2}.candidate{grid-template-columns:1fr}.actions{align-items:stretch}.actions .btn{flex:1}}
</style>
</head>
<body>
<div class="app-shell" id="appShell">
  <aside>
    <div class="brand"><div class="brand-mark">ПТ</div><div><strong>ПОИСК ТРЕНДОВ</strong><small>Instagram · TikTok · YouTube</small></div></div>
    <div class="side-note">Ищем последние 14 дней по #omni, #veo, #veo3, #ai и #ии. Речь и движение обязательны. До 10.05 сек сохраняем исходный тайминг; 10.05–15.05 сек допускается только естественное сжатие до 10 секунд. Discovery hard cap $2.80, общий target полного поиска меньше $5.</div>
    <div class="runtime-pill warn" id="runtimePill">V33 · HTML загружен · запускаю единый JS runtime…</div>
  </aside>
  <main>
    <header><div><div class="eyebrow">V33 FAIL-OPEN SINGLE RUNTIME · HARD BUDGET &lt;$5</div><h1>Что набирает обороты прямо сейчас.<br><span>Instagram + TikTok + YouTube.</span></h1></div><div class="service warn" id="serviceStatus">проверяю сервис…</div></header>
    <div class="runtime-error" id="runtimeError" hidden></div>

    <div class="api-grid">
      <div class="card api-card"><strong id="geminiApiStatus">Google AI Studio · проверяю…</strong><small id="geminiApiMeta">Speech + timing gate · максимум 150 кандидатов</small></div>
      <div class="card api-card"><strong id="apifyApiStatus">Apify · проверяю…</strong><small id="apifyApiMeta">Instagram $0.85 · TikTok $1.15 · YouTube $0.80</small></div>
      <button class="btn ghost" id="checkApis">ПРОВЕРИТЬ API</button>
    </div>

    <div class="card radar-intro"><div><b>Один frontend-runtime. Никаких legacy observer/polling слоёв.</b><span>UI всегда остаётся видимым. Динамика работает отдельно: один последовательный refresh-loop и один durable tick-driver. Перезагрузка страницы не создаёт новый поиск и не повторяет платный discovery.</span></div><div class="actions"><button class="btn" id="syncRadar">ЗАПУСТИТЬ ПОИСК</button><button class="btn danger" id="stopRadar" hidden>ОСТАНОВИТЬ</button><button class="btn ghost" id="refreshRadar">ОБНОВИТЬ ЭКРАН</button></div></div>

    <div class="card" id="progressCard">
      <div class="progress-head"><div><b id="radarStage">Готов к поиску</b><small id="radarStatusMessage">Читаю сохранённое состояние сервера.</small></div><div class="pct" id="radarProgressPct">0%</div></div>
      <div class="track"><div class="fill" id="radarProgressBar"></div></div>
      <div class="meta" id="radarEta">—</div>
      <div class="warning" id="radarWarning" hidden></div>
      <div class="stats" id="radarPipelineStats"></div>
    </div>

    <div class="section-head"><div><h2>TOP НАБИРАЮЩИХ ОБОРОТЫ</h2><span>Только прошедшие текущий V30 speech/timing + motion fail-closed профиль.</span></div><span id="topFreshness">обновляю…</span></div>
    <div class="card"><div class="rows" id="radarRows"><div class="empty">TOP пока загружается. Даже без JavaScript эта страница остаётся видимой.</div></div></div>

    <div class="section-head"><div><h2>ПУЛ КАНДИДАТОВ</h2><span>До 900 raw · максимум 150 смысловых кандидатов · 180 automatic AI ticks hard guard.</span></div><span id="candidateFreshness">обновляю…</span></div>
    <div class="card"><div id="candidateRows"><div class="empty">Кандидаты пока загружаются.</div></div></div>

    <div class="section-head"><div><h2>PRODUCTION-ПАКЕТ</h2><span>PHOTO PROMPT · VIDEO PROMPT · речь/аудио · CapCut overlay plan.</span></div></div>
    <div class="analysis-loading card" id="analysisLoading" hidden>Gemini собирает production-пакет. Основной интерфейс остаётся доступен.</div>
    <div class="analysis-result" id="analysisResult"><div class="empty">Выбери ролик в TOP и нажми «ПОЛУЧИТЬ УЛЬТРА-ПРОМПТЫ».</div></div>

    <div class="section-head"><div><h2>МЕТА ТЕКУЩЕГО ПОИСКА</h2><span>Сводка последнего завершённого/текущего run.</span></div></div>
    <div class="card meta" id="radarMeta">Мета пока не загружена.</div>
    <div class="footer-note">V33 fail-open: если JavaScript не запустится или API временно упадёт, серверный HTML и весь основной интерфейс не скрываются и не заменяются пустым экраном.</div>
  </main>
</div>
<noscript><div style="position:fixed;z-index:2147483647;left:16px;right:16px;top:16px;padding:14px;border:1px solid #8b4048;border-radius:12px;background:#2a1217;color:#ffe3e6;font:700 13px/1.5 system-ui">JavaScript отключён. Страница остаётся видимой, но автообновление и кнопки недоступны.</div></noscript>
<script>
(() => {
'use strict';
const $ = (id) => document.getElementById(id);
const PROFILE = 'frontend_v33_fail_open_single_runtime';
const LEASE_KEY = 'trend-radar-driver-v33';
const CLIENT_ID = (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') ? globalThis.crypto.randomUUID() : `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const state = { refreshBusy:false, listsBusy:false, driveBusy:false, driveEnabled:false, stopRequested:false, active:false, lastListsAt:0, consecutiveDriveErrors:0, destroyed:false, stickyRuntimeError:false };
let refreshTimer = null;
let driveTimer = null;

function esc(v=''){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function num(v){const n=Number(v||0);if(n>=1e6)return (n/1e6).toFixed(n>=1e7?0:1)+' млн';if(n>=1e3)return (n/1e3).toFixed(n>=1e5?0:1)+' тыс';return Math.round(n).toLocaleString('ru-RU');}
function eta(v){const s=Number(v);if(!Number.isFinite(s)||s<0)return '—';if(s<60)return `≈ ${Math.ceil(s)} сек`;return `≈ ${Math.ceil(s/60)} мин`;}
function nowLabel(){return new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function showRuntimeError(message,sticky=false){if(sticky)state.stickyRuntimeError=true;const el=$('runtimeError');if(!el)return;el.hidden=false;el.textContent=String(message||'Неизвестная ошибка интерфейса');const pill=$('runtimePill');if(pill){pill.className='runtime-pill bad';pill.textContent=sticky?'V33 · runtime fault зафиксирован · интерфейс сохранён':'V33 · связь/динамика восстанавливается · интерфейс сохранён';}}
function clearRuntimeError(force=false){if(state.stickyRuntimeError&&!force)return;if(force)state.stickyRuntimeError=false;const el=$('runtimeError');if(el){el.hidden=true;el.textContent='';}const pill=$('runtimePill');if(pill){pill.className='runtime-pill';pill.textContent='V33 · fail-open single runtime · JS готов';}}
window.addEventListener('error',e=>showRuntimeError(`JavaScript: ${e.message||'ошибка выполнения'}`,true));
window.addEventListener('unhandledrejection',e=>showRuntimeError(`Promise: ${e.reason?.message||e.reason||'ошибка выполнения'}`,true));

async function api(url, options={}, timeoutMs=20000){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs);
  try{
    const r=await fetch(url,{cache:'no-store',...options,headers:{Accept:'application/json',...(options.headers||{})},signal:controller.signal});
    const text=await r.text();let data=null;
    if(text.trim()){try{data=JSON.parse(text);}catch(_){throw new Error(`Некорректный JSON ${r.status} от ${url}`);}}
    if(!r.ok)throw new Error(data?.error||data?.message||`HTTP ${r.status} · ${url}`);
    return data;
  }catch(e){if(e?.name==='AbortError')throw new Error(`Таймаут ${url}`);throw e;}finally{clearTimeout(timer);}
}

function readLease(){try{return JSON.parse(localStorage.getItem(LEASE_KEY)||'null');}catch(_){return null;}}
function claimLease(){try{const now=Date.now();const x=readLease();if(x&&x.owner!==CLIENT_ID&&Number(x.expires||0)>now)return false;localStorage.setItem(LEASE_KEY,JSON.stringify({owner:CLIENT_ID,expires:now+20000}));return readLease()?.owner===CLIENT_ID;}catch(_){return true;}}
function renewLease(){try{const x=readLease();if(!x||x.owner!==CLIENT_ID)return false;localStorage.setItem(LEASE_KEY,JSON.stringify({owner:CLIENT_ID,expires:Date.now()+20000}));return true;}catch(_){return true;}}
function releaseLease(){try{const x=readLease();if(x?.owner===CLIENT_ID)localStorage.removeItem(LEASE_KEY);}catch(_){}}

function paintService(s){const el=$('serviceStatus');if(!el)return;const ready=Boolean(s?.gemini_configured&&s?.apify_configured);el.className=`service ${ready?'good':'warn'}`;el.textContent=ready?`готово · ${s.analysis_model||'Gemini'}`:'нужны/проверяются API ключи';}
function paintStatus(s,job){
  if(!s)return;const pct=Math.max(0,Math.min(100,Number(s.progress||0)));$('radarProgressPct').textContent=`${pct}%`;$('radarProgressBar').style.width=`${pct}%`;$('radarStage').textContent=s.label||'Радар';$('radarStatusMessage').textContent=s.message||'';$('radarEta').textContent=(s.stage==='done'?'ГОТОВО':s.stage==='error'?'ОШИБКА':eta(s.eta_seconds));
  const warning=$('radarWarning');if(s.warning){warning.hidden=false;warning.textContent=s.warning;}else{warning.hidden=true;warning.textContent='';}
  const d=s.details||{};const stats=[];if(d.raw!==undefined)stats.push([num(d.raw),'сырых']);if(d.numeric_candidates!==undefined)stats.push([num(d.numeric_candidates),'кандидатов']);if(d.ai_total!==undefined)stats.push([`${num(d.ai_done||0)}/${num(d.ai_total)}`,'Gemini']);if(d.quality_top!==undefined)stats.push([num(d.quality_top),'сильный TOP']);else if(d.matched!==undefined)stats.push([num(d.matched),'прошли']);$('radarPipelineStats').innerHTML=stats.map(x=>`<div class="stat"><b>${esc(x[0])}</b><span>${esc(x[1])}</span></div>`).join('');
  const phase=String(job?.phase||'');const active=Boolean(job?.active) || ['queued','discovering','preparing','ai','durability_blocked','source_start_uncertain'].includes(phase) || s.stage==='running';state.active=active;$('syncRadar').disabled=active;$('syncRadar').textContent=active?'ПОИСК ИДЁТ…':'ЗАПУСТИТЬ ПОИСК';$('stopRadar').hidden=!active;
}

function radarHtml(x,i){const platform=esc(x.platform||'');const tag=esc(x.search_term||'');const duration=Number(x.duration_sec||0);const measured=Number(x.measured_growth_per_hour||x.views_per_hour||0);const badges=[platform?`<span class="badge">${platform}</span>`:'',tag?`<span class="badge">#${tag}</span>`:'',`<span class="badge">речь + тайминг OK</span>`].join('');return `<div class="radar-row"><div class="rank">#${i+1}</div><div class="score"><b>${Number(x.viral_score_v2||0).toFixed(0)}</b><small>VIRAL</small></div><div class="row-main">${badges}<div class="row-title">${esc(x.hook||x.scene_description||'Короткая трендовая сценка')}</div><div class="row-meta">@${esc(x.creator||'')} · ${duration.toFixed(2)} сек · ${Number(x.hours_since_publish||0).toFixed(1)} ч назад</div><div class="row-desc">${esc(x.scene_description||x.priority_reason||'')}</div></div><div class="metric"><b>${num(x.views)}</b><small>просмотров</small></div><div class="metric"><b>${num(measured)}/ч</b><small>скорость</small></div><div class="row-actions actions"><a class="btn ghost" href="${esc(x.post_url||'#')}" target="_blank" rel="noopener noreferrer">ОРИГИНАЛ</a><button class="btn" data-analyze="${Number(x.id||0)}">УЛЬТРА-ПРОМПТЫ</button></div></div>`;}
function candidateHtml(x,i){const status=x.ai_match?'РЕЧЬ + ТАЙМИНГ OK':(x.ai_checked?'ОТКЛОНЁН GEMINI':'GEMINI ПРОВЕРЯЕТ');const cls=x.ai_match?'ok':(x.ai_checked?'no':'');return `<div class="candidate"><div><b>#${i+1} · ${esc(x.platform||'')} · @${esc(x.creator||'')}</b><span>${Number(x.duration_sec||0).toFixed(2)} сек · ${Number(x.hours_since_publish||0).toFixed(1)} ч назад · #${esc(x.search_term||'')}</span></div><div><b>${num(x.views)}</b><span>${num(x.likes)} лайков · ${num(x.views_per_hour)}/ч</span></div><div class="candidate-state ${cls}">${status}</div></div>`;}

async function refreshCore(){
  if(state.refreshBusy||state.destroyed)return;state.refreshBusy=true;
  try{
    const [statusR,radarR,jobR]=await Promise.allSettled([api('/api/status'),api('/api/radar/status'),api('/api/radar/job')]);
    const fulfilled=[statusR,radarR,jobR].filter(x=>x.status==='fulfilled').length;
    if(statusR.status==='fulfilled')paintService(statusR.value);if(radarR.status==='fulfilled')paintStatus(radarR.value,jobR.status==='fulfilled'?jobR.value:null);else if(jobR.status==='fulfilled')paintStatus({stage:jobR.value?.active?'running':'idle',label:jobR.value?.active?'Поиск выполняется':'Готов к поиску',message:jobR.value?.message||'',progress:0},jobR.value);
    if(jobR.status==='fulfilled'&&jobR.value?.active&&!state.stopRequested){state.driveEnabled=true;scheduleDrive(300);}
    if(fulfilled===0){const first=[statusR,radarR,jobR].find(x=>x.status==='rejected');showRuntimeError(`Связь с сервером временно потеряна: ${first?.reason?.message||'нет ответа'}`);}else clearRuntimeError();
  }catch(e){showRuntimeError(`Обновление статуса: ${e.message}`);}finally{state.refreshBusy=false;scheduleRefresh();}
}

async function refreshLists(force=false){
  if(state.listsBusy||state.destroyed)return;if(!force&&Date.now()-state.lastListsAt<10000)return;state.listsBusy=true;
  try{
    const [topR,candR,metaR]=await Promise.allSettled([api('/api/radar',{},25000),api('/api/radar/candidates',{},25000),api('/api/radar/meta',{},20000)]);
    if(topR.status==='fulfilled'){const rows=Array.isArray(topR.value)?topR.value:[];$('radarRows').innerHTML=rows.length?rows.map(radarHtml).join(''):'<div class="empty">Сильный TOP пока формируется.</div>';$('topFreshness').textContent=`обновлено ${nowLabel()} · ${rows.length} шт.`;}
    if(candR.status==='fulfilled'){const rows=Array.isArray(candR.value)?candR.value:[];$('candidateRows').innerHTML=rows.length?rows.slice(0,180).map(candidateHtml).join(''):'<div class="empty">Кандидаты пока не сформированы.</div>';$('candidateFreshness').textContent=`обновлено ${nowLabel()} · ${rows.length} шт.`;}
    if(metaR.status==='fulfilled')$('radarMeta').textContent=metaR.value?JSON.stringify(metaR.value,null,2):'Мета появится после завершения поиска.';
    state.lastListsAt=Date.now();
  }catch(e){showRuntimeError(`Обновление данных: ${e.message}`);}finally{state.listsBusy=false;}
}

function scheduleRefresh(){clearTimeout(refreshTimer);if(state.destroyed)return;refreshTimer=setTimeout(async()=>{await refreshLists(false);await refreshCore();},document.hidden?15000:(state.active?4500:9000));}
function scheduleDrive(delay=900){clearTimeout(driveTimer);if(!state.driveEnabled||state.stopRequested||state.destroyed)return;driveTimer=setTimeout(driveOnce,delay);}
async function driveOnce(){
  if(state.driveBusy||state.stopRequested||!state.driveEnabled)return;if(!claimLease()){scheduleDrive(3000);return;}state.driveBusy=true;renewLease();
  try{const d=await api('/api/radar/tick',{method:'POST'},160000);state.consecutiveDriveErrors=0;renewLease();state.driveEnabled=Boolean(d?.active);await refreshCore();await refreshLists(true);if(state.driveEnabled)scheduleDrive(d?.busy?1800:(d?.transient_error?6000:2200));else releaseLease();}
  catch(e){state.consecutiveDriveErrors+=1;showRuntimeError(`Tick ${state.consecutiveDriveErrors}/6: ${e.message}`);if(state.consecutiveDriveErrors>=6){state.driveEnabled=false;releaseLease();}else scheduleDrive(Math.min(12000,2500+state.consecutiveDriveErrors*1500));}
  finally{state.driveBusy=false;}
}

async function startRadar(){const b=$('syncRadar');b.disabled=true;b.textContent='ПОДКЛЮЧАЮСЬ…';state.stopRequested=false;state.consecutiveDriveErrors=0;try{const d=await api('/api/radar/sync',{method:'POST'},30000);if(d?.accepted===false||d?.stop_pending)throw new Error(d?.message||'Сервер ещё завершает предыдущую остановку');state.driveEnabled=true;claimLease();await refreshCore();await refreshLists(true);scheduleDrive(250);}catch(e){showRuntimeError(`Запуск: ${e.message}`);b.disabled=false;b.textContent='ЗАПУСТИТЬ ПОИСК';}}
async function stopRadar(){const b=$('stopRadar');b.disabled=true;b.textContent='ОСТАНАВЛИВАЮ…';state.stopRequested=true;state.driveEnabled=false;clearTimeout(driveTimer);releaseLease();try{for(let i=0;i<8;i++){const d=await api('/api/radar/stop',{method:'POST'},20000);if(d?.active===false||d?.cancelled||d?.already_stopped)break;await new Promise(r=>setTimeout(r,1200));}await refreshCore();await refreshLists(true);}catch(e){showRuntimeError(`Остановка: ${e.message}`);}finally{state.stopRequested=false;b.disabled=false;b.textContent='ОСТАНОВИТЬ';}}
async function checkApis(){const b=$('checkApis');b.disabled=true;b.textContent='ПРОВЕРЯЮ…';try{const d=await api('/api/diagnostics',{},30000);const g=d?.gemini||{};const a=d?.apify||{};$('geminiApiStatus').textContent=`Google AI Studio · ${g.ok?'✓ работает':'✕ нет подключения'}`;$('geminiApiMeta').textContent=[g.model,g.label,g.latency_ms?`${g.latency_ms} мс`:null].filter(Boolean).join(' · ')||'Speech + timing gate';$('apifyApiStatus').textContent=`Apify · ${a.ok?'✓ работает':'✕ нет подключения'}`;$('apifyApiMeta').textContent=[a.account,a.label,a.latency_ms?`${a.latency_ms} мс`:null].filter(Boolean).join(' · ')||'Discovery hard cap $2.80';}catch(e){showRuntimeError(`Диагностика API: ${e.message}`);}finally{b.disabled=false;b.textContent='ПРОВЕРИТЬ API';}}

function block(title,text){return `<div class="analysis-block"><h3>${esc(title)}</h3><button class="btn ghost copy" data-copy="${encodeURIComponent(String(text||''))}">КОПИРОВАТЬ</button><pre>${esc(text||'—')}</pre></div>`;}
function renderAnalysis(d,model=''){const video=typeof d.block_3_video==='string'?d.block_3_video:JSON.stringify(d.block_3_video||{},null,2);const audio=typeof d.block_4_audio==='string'?d.block_4_audio:JSON.stringify(d.block_4_audio||{},null,2);const overlays=JSON.stringify(d.capcut_overlay_plan||{},null,2);const director=JSON.stringify(d.block_0_director||{},null,2);const compliance=JSON.stringify(d.block_2_compliance||{},null,2);const publication=JSON.stringify(d.block_5_publication||{},null,2);const whole=[`MODEL: ${model}`,`\nБЛОК 0\n${director}`,`\nБЛОК 1 PHOTO PROMPT\n${d.block_1_frame0_prompt||''}`,`\nБЛОК 2\n${compliance}`,`\nБЛОК 3 VIDEO PROMPT\n${video}`,`\nБЛОК 4 AUDIO\n${audio}`,`\nБЛОК 5 PUBLICATION\n${publication}`,`\nCAPCUT OVERLAY PLAN\n${overlays}`].join('\n');$('analysisResult').innerHTML=block('ВЕСЬ PRODUCTION-ПАКЕТ',whole)+block('PHOTO PROMPT — FRAME 0',d.block_1_frame0_prompt||'')+block('VIDEO PROMPT',video)+block('РУССКАЯ РЕЧЬ И АУДИО',audio)+block('CAPCUT OVERLAY PLAN',overlays);}
async function analyze(id,button){if(!id)return;const loading=$('analysisLoading');loading.hidden=false;const old=button.textContent;button.disabled=true;button.textContent='GEMINI АНАЛИЗИРУЕТ…';try{const data=await api(`/api/radar/${id}/analyze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owned_or_licensed:false})},190000);renderAnalysis(data?.result||{},data?.model||'');$('analysisResult').scrollIntoView({behavior:'smooth',block:'start'});}catch(e){showRuntimeError(`Production-анализ: ${e.message}`);}finally{loading.hidden=true;button.disabled=false;button.textContent=old;}}

$('syncRadar').addEventListener('click',startRadar);$('stopRadar').addEventListener('click',stopRadar);$('refreshRadar').addEventListener('click',async()=>{await refreshLists(true);await refreshCore();});$('checkApis').addEventListener('click',checkApis);
document.addEventListener('click',async e=>{const a=e.target.closest('[data-analyze]');if(a){await analyze(Number(a.dataset.analyze),a);return;}const c=e.target.closest('[data-copy]');if(c){const text=decodeURIComponent(c.dataset.copy||'');try{await navigator.clipboard.writeText(text);c.textContent='СКОПИРОВАНО';setTimeout(()=>c.textContent='КОПИРОВАТЬ',1200);}catch(_){showRuntimeError('Не удалось скопировать в буфер обмена');}}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshLists(true);refreshCore();}});window.addEventListener('online',()=>{refreshLists(true);refreshCore();});window.addEventListener('beforeunload',()=>{state.destroyed=true;clearTimeout(refreshTimer);clearTimeout(driveTimer);releaseLease();});

window.__V33_LOADED__=true;document.body.dataset.frontend=PROFILE;const pill=$('runtimePill');pill.className='runtime-pill';pill.textContent='V33 · fail-open single runtime · JS готов';
refreshLists(true);refreshCore();setTimeout(checkApis,600);
})();
</script>
</body>
</html>'''

HTML_BYTES = HTML.encode("utf-8")
HTML_SHA256 = hashlib.sha256(HTML_BYTES).hexdigest()[:16]


def frontend_health_payload() -> dict:
    return {
        "ok": True,
        "profile": PROFILE,
        "html_bytes": len(HTML_BYTES),
        "html_sha256": HTML_SHA256,
        "external_static_dependencies": 0,
        "root_db_dependency": False,
        "fail_open_dom": True,
        "single_js_runtime": True,
        "legacy_client_scripts": 0,
        "mutation_observers": 0,
        "parallel_polling_layers": 0,
        "runtime_error_surface": True,
    }


def install_frontend_v33(app) -> dict:
    if '/static/' in HTML:
        raise RuntimeError("V33 frontend must not depend on /static assets")
    if 'MutationObserver' in HTML:
        raise RuntimeError("V33 frontend must not contain MutationObserver")
    if HTML.count('<script>') != 1 or HTML.count('</script>') != 1:
        raise RuntimeError("V33 frontend must contain exactly one application script")
    if len(HTML_BYTES) < 20_000:
        raise RuntimeError(f"V33 HTML unexpectedly small: {len(HTML_BYTES)} bytes")

    @app.before_request
    def _v33_fail_open_root():
        if request.method == 'GET' and request.path == '/':
            response = Response(HTML_BYTES, status=200, content_type='text/html; charset=utf-8')
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            response.headers['X-Frontend-Profile'] = PROFILE
            response.headers['X-Frontend-Bytes'] = str(len(HTML_BYTES))
            response.headers['X-Frontend-SHA256'] = HTML_SHA256
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'same-origin'
            return response
        return None

    if 'frontend_health_v33' not in app.view_functions:
        app.add_url_rule(
            '/api/frontend-health',
            endpoint='frontend_health_v33',
            view_func=lambda: jsonify(frontend_health_payload()),
            methods=['GET'],
        )

    return frontend_health_payload()
