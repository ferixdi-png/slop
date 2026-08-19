"""V35 browser safety patch: opening/reloading the site is read-only.

This patch is deliberately idempotent. Product wording can evolve in V34/V36
without making a Render deploy crash. Security/behavioural JS invariants remain
strict: if neither the old code nor the already-patched code can be found, startup
fails rather than silently re-introducing auto-resume.
"""

from __future__ import annotations

import hashlib

import frontend_broad_v34 as broad
import frontend_failopen_v33 as v33

PROFILE = broad.PROFILE


def _replace_guarded(text: str, old: str, new: str, label: str, *, critical: bool = True) -> str:
    """Replace one old fragment, accept an already-patched fragment, optionally tolerate copy drift."""
    if new in text:
        return text
    count = text.count(old)
    if count == 1:
        return text.replace(old, new, 1)
    if critical:
        raise RuntimeError(
            f"V35 frontend invariant {label}: neither expected source nor patched target found "
            f"(source_count={count})"
        )
    return text


def _sync_modules(html: str) -> dict:
    html_bytes = html.encode("utf-8")
    html_sha = hashlib.sha256(html_bytes).hexdigest()[:16]
    broad.HTML = html
    broad.HTML_BYTES = html_bytes
    broad.HTML_SHA256 = html_sha
    v33.PROFILE = PROFILE
    v33.HTML = html
    v33.HTML_BYTES = html_bytes
    v33.HTML_SHA256 = html_sha
    return {
        "profile": PROFILE,
        "html_bytes": len(html_bytes),
        "html_sha256": html_sha,
        "manual_start_only": True,
        "auto_tick_on_load": False,
        "auto_diagnostics_on_load": False,
        "driver_token_storage": "memory_only",
        "auth_loss_policy": "stop_driver_on_first_409",
        "patch_idempotent": True,
    }


def patch_frontend_v35() -> dict:
    html = broad.HTML

    # Cosmetic/product copy must never be capable of crashing production startup.
    html = _replace_guarded(
        html,
        "Цель — дать 50–100 сильнейших вариантов, чтобы ты сам выбрал механику. Отсутствие речи, другой тайминг или временная ошибка Gemini больше не скрывают ролик. Перезагрузка страницы не повторяет платный discovery.",
        "Цель — дать 50–100 сильнейших вариантов, чтобы ты сам выбрал механику. Открытие сайта, F5, новая вкладка и новый деплой НИКОГДА не запускают и не продолжают поиск. Продолжение возможно только после явного нажатия кнопки «ЗАПУСТИТЬ/ПРОДОЛЖИТЬ ПОИСК».",
        "manual-start-copy",
        critical=False,
    )
    html = _replace_guarded(
        html,
        "V34 BROAD TREND POOL · 50–100 ВАРИАНТОВ · HARD BUDGET &lt;$5",
        "V34 BROAD TREND POOL · MANUAL START ONLY · 50–100 ВАРИАНТОВ · HARD BUDGET &lt;$5",
        "eyebrow",
        critical=False,
    )

    # Everything below is behavioural/security-critical. An already-patched target
    # is accepted, but unexplained source drift fails startup loudly.
    html = _replace_guarded(
        html,
        "const state = { refreshBusy:false, listsBusy:false, driveBusy:false, driveEnabled:false, stopRequested:false, active:false, lastListsAt:0, consecutiveDriveErrors:0, destroyed:false, stickyRuntimeError:false };",
        "const state = { refreshBusy:false, listsBusy:false, driveBusy:false, driveEnabled:false, driverToken:'', manualSessionStarted:false, stopRequested:false, active:false, lastListsAt:0, consecutiveDriveErrors:0, destroyed:false, stickyRuntimeError:false };",
        "state-token",
    )
    html = _replace_guarded(
        html,
        "const r=await fetch(url,{cache:'no-store',...options,headers:{Accept:'application/json',...(options.headers||{})},signal:controller.signal});",
        "const r=await fetch(url,{cache:'no-store',...options,headers:{Accept:'application/json',...(state.driverToken?{'X-Radar-Driver-Token':state.driverToken}:{}),...(options.headers||{})},signal:controller.signal});",
        "driver-token-header",
    )
    html = _replace_guarded(
        html,
        "if(!r.ok)throw new Error(data?.error||data?.message||`HTTP ${r.status} · ${url}`);",
        "if(!r.ok){const err=new Error(data?.error||data?.message||`HTTP ${r.status} · ${url}`);err.status=r.status;err.payload=data;throw err;}",
        "http-status-on-error",
    )
    html = _replace_guarded(
        html,
        "  if(!s)return;const pct=Math.max(0,Math.min(100,Number(s.progress||0)));$('radarProgressPct').textContent=`${pct}%`;$('radarProgressBar').style.width=`${pct}%`;$('radarStage').textContent=s.label||'Радар';$('radarStatusMessage').textContent=s.message||'';$('radarEta').textContent=(s.stage==='done'?'ГОТОВО':s.stage==='error'?'ОШИБКА':eta(s.eta_seconds));",
        "  if(!s)return;const paused=Boolean(job?.paused||job?.manual_start_required);const pct=Math.max(0,Math.min(100,Number(s.progress||0)));$('radarProgressPct').textContent=`${pct}%`;$('radarProgressBar').style.width=`${pct}%`;if(paused){$('radarStage').textContent='Поиск приостановлен';$('radarStatusMessage').textContent=job?.message||'Незавершённый поиск сохранён. Для продолжения нажмите кнопку вручную.';$('radarEta').textContent='—';}else{$('radarStage').textContent=s.label||'Радар';$('radarStatusMessage').textContent=s.message||'';$('radarEta').textContent=(s.stage==='done'?'ГОТОВО':s.stage==='error'?'ОШИБКА':eta(s.eta_seconds));}",
        "paused-status",
    )
    html = _replace_guarded(
        html,
        "  const phase=String(job?.phase||'');const active=Boolean(job?.active) || ['queued','discovering','preparing','ai','durability_blocked','source_start_uncertain'].includes(phase) || s.stage==='running';state.active=active;$('syncRadar').disabled=active;$('syncRadar').textContent=active?'ПОИСК ИДЁТ…':'ЗАПУСТИТЬ ПОИСК';$('stopRadar').hidden=!active;",
        "  const phase=String(job?.phase||'');const active=Boolean(job?.active)&&!paused;state.active=active;$('syncRadar').disabled=active;$('syncRadar').textContent=active?'ПОИСК ИДЁТ…':(paused?'ПРОДОЛЖИТЬ ПОИСК':'ЗАПУСТИТЬ ПОИСК');$('stopRadar').hidden=!(active||paused);$('stopRadar').textContent=paused?'СБРОСИТЬ СОХРАНЁННЫЙ ПОИСК':'ОСТАНОВИТЬ';",
        "paint-active",
    )
    html = _replace_guarded(
        html,
        "    if(jobR.status==='fulfilled'&&jobR.value?.active&&!state.stopRequested){state.driveEnabled=true;scheduleDrive(300);}",
        "    /* V35 invariant: read-only refresh NEVER arms or resumes the tick driver. */",
        "remove-auto-drive",
    )
    html = _replace_guarded(
        html,
        "async function driveOnce(){\n  if(state.driveBusy||state.stopRequested||!state.driveEnabled)return;if(!claimLease()){scheduleDrive(3000);return;}state.driveBusy=true;renewLease();\n  try{const d=await api('/api/radar/tick',{method:'POST'},160000);state.consecutiveDriveErrors=0;renewLease();state.driveEnabled=Boolean(d?.active);await refreshCore();await refreshLists(true);if(state.driveEnabled)scheduleDrive(d?.busy?1800:(d?.transient_error?6000:2200));else releaseLease();}\n  catch(e){state.consecutiveDriveErrors+=1;showRuntimeError(`Tick ${state.consecutiveDriveErrors}/6: ${e.message}`);if(state.consecutiveDriveErrors>=6){state.driveEnabled=false;releaseLease();}else scheduleDrive(Math.min(12000,2500+state.consecutiveDriveErrors*1500));}\n  finally{state.driveBusy=false;}\n}",
        "async function driveOnce(){\n  if(state.driveBusy||state.stopRequested||!state.driveEnabled)return;if(!claimLease()){scheduleDrive(3000);return;}state.driveBusy=true;renewLease();\n  try{const d=await api('/api/radar/tick',{method:'POST'},160000);state.consecutiveDriveErrors=0;renewLease();state.driveEnabled=Boolean(d?.active);await refreshCore();await refreshLists(true);if(state.driveEnabled)scheduleDrive(d?.busy?1800:(d?.transient_error?6000:2200));else releaseLease();}\n  catch(e){if(Number(e?.status)===409){state.driveEnabled=false;state.driverToken='';state.manualSessionStarted=false;state.consecutiveDriveErrors=0;releaseLease();clearRuntimeError(true);await refreshCore();await refreshLists(true);return;}state.consecutiveDriveErrors+=1;showRuntimeError(`Tick ${state.consecutiveDriveErrors}/6: ${e.message}`);if(state.consecutiveDriveErrors>=6){state.driveEnabled=false;releaseLease();}else scheduleDrive(Math.min(12000,2500+state.consecutiveDriveErrors*1500));}\n  finally{state.driveBusy=false;}\n}",
        "stop-on-auth-loss",
    )
    html = _replace_guarded(
        html,
        "async function startRadar(){const b=$('syncRadar');b.disabled=true;b.textContent='ПОДКЛЮЧАЮСЬ…';state.stopRequested=false;state.consecutiveDriveErrors=0;try{const d=await api('/api/radar/sync',{method:'POST'},30000);if(d?.accepted===false||d?.stop_pending)throw new Error(d?.message||'Сервер ещё завершает предыдущую остановку');state.driveEnabled=true;claimLease();await refreshCore();await refreshLists(true);scheduleDrive(250);}catch(e){showRuntimeError(`Запуск: ${e.message}`);b.disabled=false;b.textContent='ЗАПУСТИТЬ ПОИСК';}}",
        "async function startRadar(){const b=$('syncRadar');b.disabled=true;b.textContent='ПОДКЛЮЧАЮСЬ…';state.stopRequested=false;state.consecutiveDriveErrors=0;try{const d=await api('/api/radar/sync',{method:'POST',headers:{'X-Radar-Manual-Start':'click-v35'}},30000);if(d?.accepted===false||d?.stop_pending)throw new Error(d?.message||'Сервер ещё завершает предыдущую остановку');if(!d?.driver_token)throw new Error('Сервер не выдал ручной driver-token');state.driverToken=String(d.driver_token);state.manualSessionStarted=true;state.driveEnabled=true;claimLease();await refreshCore();await refreshLists(true);scheduleDrive(250);}catch(e){state.driverToken='';state.manualSessionStarted=false;state.driveEnabled=false;showRuntimeError(`Запуск: ${e.message}`);b.disabled=false;b.textContent='ЗАПУСТИТЬ ПОИСК';}}",
        "explicit-start-token",
    )
    html = _replace_guarded(
        html,
        "async function stopRadar(){const b=$('stopRadar');b.disabled=true;b.textContent='ОСТАНАВЛИВАЮ…';state.stopRequested=true;state.driveEnabled=false;clearTimeout(driveTimer);releaseLease();try{for(let i=0;i<8;i++){const d=await api('/api/radar/stop',{method:'POST'},20000);if(d?.active===false||d?.cancelled||d?.already_stopped)break;await new Promise(r=>setTimeout(r,1200));}await refreshCore();await refreshLists(true);}catch(e){showRuntimeError(`Остановка: ${e.message}`);}finally{state.stopRequested=false;b.disabled=false;b.textContent='ОСТАНОВИТЬ';}}",
        "async function stopRadar(){const b=$('stopRadar');b.disabled=true;b.textContent='ОСТАНАВЛИВАЮ…';state.stopRequested=true;state.driveEnabled=false;clearTimeout(driveTimer);releaseLease();try{for(let i=0;i<8;i++){const d=await api('/api/radar/stop',{method:'POST'},20000);if(d?.active===false||d?.cancelled||d?.already_stopped)break;await new Promise(r=>setTimeout(r,1200));}state.driverToken='';state.manualSessionStarted=false;await refreshCore();await refreshLists(true);}catch(e){showRuntimeError(`Остановка: ${e.message}`);}finally{state.stopRequested=false;b.disabled=false;b.textContent='ОСТАНОВИТЬ';}}",
        "stop-clears-token",
    )
    html = _replace_guarded(
        html,
        "window.addEventListener('beforeunload',()=>{state.destroyed=true;clearTimeout(refreshTimer);clearTimeout(driveTimer);releaseLease();});",
        "window.addEventListener('beforeunload',()=>{state.destroyed=true;state.driveEnabled=false;state.driverToken='';state.manualSessionStarted=false;clearTimeout(refreshTimer);clearTimeout(driveTimer);releaseLease();});",
        "reload-clears-token",
    )
    html = _replace_guarded(
        html,
        "refreshLists(true);refreshCore();setTimeout(checkApis,600);",
        "refreshLists(true);refreshCore();",
        "no-auto-diagnostics",
    )
    html = _replace_guarded(
        html,
        "window.__V34_LOADED__=true;",
        "window.__V34_LOADED__=true;window.__V35_MANUAL_START__=true;",
        "manual-runtime-marker",
    )

    # Cosmetic runtime label, safe under wording drift.
    if "V34 · broad trend pool · MANUAL START ONLY · JS готов" not in html:
        html = html.replace(
            "V34 · broad trend pool · fail-open JS готов",
            "V34 · broad trend pool · MANUAL START ONLY · JS готов",
        )

    # Final invariants are stronger than individual replacements and make repeated
    # calls deterministic.
    required_fragments = (
        "window.__V35_MANUAL_START__=true",
        "X-Radar-Driver-Token",
        "read-only refresh NEVER arms or resumes the tick driver",
        "Number(e?.status)===409",
        "state.driverToken=''",
    )
    missing = [fragment for fragment in required_fragments if fragment not in html]
    if missing:
        raise RuntimeError(f"V35 final frontend invariants missing: {missing}")
    if "jobR.value?.active&&!state.stopRequested){state.driveEnabled=true" in html:
        raise RuntimeError("V35 invariant failed: page-load auto-drive is still present")
    if "setTimeout(checkApis,600)" in html:
        raise RuntimeError("V35 invariant failed: page-load diagnostics mutation is still present")

    return _sync_modules(html)
