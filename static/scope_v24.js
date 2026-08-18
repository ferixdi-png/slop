(() => {
  // Keep the stable UI renderer, but surface the final V24/V26 semantics.
  const originalRadarCard = window.radarCard;
  if (typeof originalRadarCard === 'function') {
    window.radarCard = function v24RadarCard(row, index) {
      const copy = {...(row || {})};
      const measured = Number(copy.measured_growth_per_hour || 0);
      if (measured > 0) copy.views_per_hour = measured;
      let html = originalRadarCard(copy, index);
      if (measured > 0) {
        const acceleration = Number(copy.growth_acceleration || 0);
        const badge = `<div class="anomaly hot">замер между поисками: <b>${Math.round(measured).toLocaleString('ru-RU')}/ч</b>${acceleration > 0 ? ` · ускорение ×${acceleration.toFixed(acceleration >= 10 ? 0 : 1)}` : ''}</div>`;
        html = html.replace('</div>\n    <div class="radar-number">', `${badge}</div>\n    <div class="radar-number">`);
      }
      return html;
    };
  }

  function replaceLabels() {
    const stats = document.getElementById('radarPipelineStats');
    if (stats) {
      stats.querySelectorAll('span').forEach(span => {
        const text = span.textContent || '';
        if (/AI проверка|смысловая проверка/i.test(text)) span.textContent = 'MP4 проверка';
        if (/AI совпадений/i.test(text)) span.textContent = 'прошли проверку';
      });
    }

    const candidates = document.getElementById('candidateRows');
    if (candidates) {
      candidates.querySelectorAll('.candidate-state').forEach(el => {
        const text = (el.textContent || '').trim();
        if (text === 'ПРОШЁЛ AI') el.textContent = 'ПРОШЁЛ MP4';
        else if (text === 'НЕ ПРОШЁЛ AI') el.textContent = 'ОТКЛОНЁН';
        else if (text === 'ЖДЁТ AI') el.textContent = 'ЖДЁТ MP4';
      });
    }
  }

  replaceLabels();
  const observer = new MutationObserver(replaceLabels);
  for (const id of ['radarPipelineStats', 'candidateRows']) {
    const node = document.getElementById(id);
    if (node) observer.observe(node, {childList: true, subtree: true});
  }
})();
