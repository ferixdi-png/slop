(() => {
  // Keep the existing UI code stable; this tiny layer only updates terminology
  // for the final V24 scope and shows measured cross-run growth when available.
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

  const replaceLabels = () => {
    const stats = document.getElementById('radarPipelineStats');
    if (stats) {
      stats.querySelectorAll('span').forEach(span => {
        if (/AI проверка|смысловая проверка/i.test(span.textContent || '')) span.textContent = 'MP4 проверка';
        if (/AI совпадений/i.test(span.textContent || '')) span.textContent = 'прошли проверку';
      });
    }
  };

  replaceLabels();
  const observer = new MutationObserver(replaceLabels);
  const stats = document.getElementById('radarPipelineStats');
  if (stats) observer.observe(stats, {childList: true, subtree: true});
})();
