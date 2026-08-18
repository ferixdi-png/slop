(() => {
  const DIRECT_MAX = 10.05;
  const TARGET = 10.0;

  // Keep the stable renderer but surface measured momentum and V27 compression.
  const originalRadarCard = window.radarCard;
  if (typeof originalRadarCard === 'function') {
    window.radarCard = function strictRadarCard(row, index) {
      const copy = {...(row || {})};
      const measured = Number(copy.measured_growth_per_hour || 0);
      if (measured > 0) copy.views_per_hour = measured;
      let html = originalRadarCard(copy, index);

      const badges = [];
      if (measured > 0) {
        const acceleration = Number(copy.growth_acceleration || 0);
        badges.push(`<div class="anomaly hot">замер между поисками: <b>${Math.round(measured).toLocaleString('ru-RU')}/ч</b>${acceleration > 0 ? ` · ускорение ×${acceleration.toFixed(acceleration >= 10 ? 0 : 1)}` : ''}</div>`);
      }

      const duration = Number(copy.duration_sec || 0);
      if (duration > DIRECT_MAX) {
        badges.push(`<div class="anomaly hot">сжатие механики: <b>${duration.toFixed(2)} сек → ${TARGET.toFixed(0)} сек</b></div>`);
      }

      if (badges.length) {
        html = html.replace('</div>\n    <div class="radar-number">', `${badges.join('')}</div>\n    <div class="radar-number">`);
      }
      return html;
    };
  }

  function replaceStaticCopy() {
    document.title = 'ПОИСК ТРЕНДОВ — #OMNI + #VEO + #VEO3';

    const brand = document.querySelector('.brand small');
    if (brand) brand.textContent = 'радар #omni + #veo + #veo3';

    const hero = document.querySelector('header h1 span');
    if (hero) hero.textContent = '#omni + #veo + #veo3.';

    const section = document.querySelector('.section-title');
    if (section) {
      const step = section.querySelector('.step');
      const h2 = section.querySelector('h2');
      const muted = section.querySelector('.muted');
      if (step) step.textContent = '3 ХЕШТЕГА · STRICT';
      if (h2) h2.textContent = '#omni + #veo + #veo3 · исходники до 15 сек · финал максимум 10 сек';
      if (muted) muted.textContent = 'до 250 Reels на каждый тег · только если тег реально есть в посте';
    }

    const apiMeta = document.getElementById('apifyApiMeta');
    if (apiMeta) apiMeta.textContent = 'Строгий сбор: #omni / #veo / #veo3 + проверка реального hashtag в самом посте';

    const intro = document.querySelector('.radar-intro');
    if (intro) {
      const title = intro.querySelector('b');
      const text = intro.querySelector('span');
      if (title) title.textContent = 'Только реальные #omni / #veo / #veo3. Никаких соседних и похожих роликов';
      if (text) text.textContent = 'Три отдельных Apify-источника собирают до 250 Reels на каждый тег → затем каждый сырой объект проверяется повторно: в hashtags/caption самого поста обязан реально присутствовать #omni, #veo или #veo3 → записи без одного из трёх тегов удаляются ещё до БД и до пула кандидатов → исходники до 10.05 сек проходят локальную MP4 + motion проверку → исходники 10.05–15.05 сек остаются только если отдельная проверка подтверждает, что setup, ключевое действие/реплики, payoff и нужную реакцию можно естественно уложить ровно в 10 секунд без ускоренной речи и потери механики → ranking ставит наверх самый быстрый текущий рост просмотров.';
    }

    const mini = [...document.querySelectorAll('.section-mini-title')];
    if (mini[0]) {
      const b = mini[0].querySelector('b');
      const span = mini[0].querySelector('span');
      if (b) b.textContent = 'TOP НАБИРАЮЩИХ ОБОРОТЫ';
      if (span) span.textContent = '#omni + #veo + #veo3 · источник ≤15.05 сек · >10 сек только если ужимается до 10 · strict hashtag';
    }
    if (mini[1]) {
      const b = mini[1].querySelector('b');
      const span = mini[1].querySelector('span');
      if (b) b.textContent = 'Пул #omni + #veo + #veo3';
      if (span) span.textContent = 'до 750 raw Reels; нерелевантные по реальному hashtag отбрасываются до показа';
    }
  }

  function candidateDuration(row) {
    const meta = row.querySelector('div span');
    const match = String(meta?.textContent || '').match(/([0-9]+(?:[.,][0-9]+)?)\s*сек/i);
    return match ? Number(match[1].replace(',', '.')) : 0;
  }

  function replaceDynamicLabels() {
    const stats = document.getElementById('radarPipelineStats');
    if (stats) {
      stats.querySelectorAll('span').forEach(span => {
        const text = span.textContent || '';
        if (/AI проверка|смысловая проверка/i.test(text)) span.textContent = 'MP4 / сжатие';
        if (/AI совпадений/i.test(text)) span.textContent = 'прошли проверку';
      });
    }

    const candidates = document.getElementById('candidateRows');
    if (candidates) {
      candidates.querySelectorAll('.candidate-row').forEach(row => {
        const duration = candidateDuration(row);
        const state = row.querySelector('.candidate-state');
        if (!state) return;
        const text = (state.textContent || '').trim();

        if (duration > DIRECT_MAX) {
          if (text === 'ПРОШЁЛ AI' || text === 'ПРОШЁЛ MP4' || text === 'ПРОШЁЛ ПРОВЕРКУ') {
            state.textContent = `${duration.toFixed(2)} СЕК → МОЖНО УЖАТЬ ДО 10`;
          } else if (text === 'НЕ ПРОШЁЛ AI' || text === 'ОТКЛОНЁН') {
            state.textContent = 'НЕ УЖИМАЕТСЯ ДО 10 СЕК';
          } else if (text === 'ЖДЁТ AI' || text === 'ЖДЁТ MP4' || text === 'ЖДЁТ ПРОВЕРКУ') {
            state.textContent = 'ПРОВЕРЯЮ СЖАТИЕ ДО 10 СЕК';
          }
        } else {
          if (text === 'ПРОШЁЛ AI') state.textContent = 'ПРОШЁЛ MP4';
          else if (text === 'НЕ ПРОШЁЛ AI') state.textContent = 'ОТКЛОНЁН';
          else if (text === 'ЖДЁТ AI') state.textContent = 'ЖДЁТ MP4';
        }
      });
    }
  }

  replaceStaticCopy();
  replaceDynamicLabels();

  const observer = new MutationObserver(() => {
    replaceStaticCopy();
    replaceDynamicLabels();
  });
  for (const id of ['radarPipelineStats', 'candidateRows', 'radarRows']) {
    const node = document.getElementById(id);
    if (node) observer.observe(node, {childList: true, subtree: true});
  }
})();