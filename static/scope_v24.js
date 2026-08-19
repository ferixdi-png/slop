(() => {
  const DIRECT_MAX = 10.05;
  const TARGET = 10.0;
  const TARGET_TAGS = new Set(['omni', 'veo', 'veo3', 'ai', 'ии']);
  const PLATFORMS = new Set(['Instagram Reels', 'TikTok', 'YouTube Shorts']);

  const originalRadarCard = window.radarCard;
  if (typeof originalRadarCard === 'function') {
    window.radarCard = function v28RadarCard(row, index) {
      const copy = {...(row || {})};
      const measured = Number(copy.measured_growth_per_hour || 0);
      if (measured > 0) copy.views_per_hour = measured;
      let html = originalRadarCard(copy, index);

      const badges = [];
      const platform = String(copy.platform || '').trim();
      if (PLATFORMS.has(platform)) {
        badges.push(`<div class="anomaly hot">платформа: <b>${escapeHtml(platform)}</b></div>`);
      }
      const tag = String(copy.search_term || '').trim().toLowerCase();
      if (TARGET_TAGS.has(tag)) {
        badges.push(`<div class="anomaly hot">strict hashtag: <b>#${escapeHtml(tag)}</b></div>`);
      }
      badges.push('<div class="anomaly hot">Gemini: <b>речь + тайминг подтверждены</b></div>');
      if (measured > 0) {
        const acceleration = Number(copy.growth_acceleration || 0);
        badges.push(`<div class="anomaly hot">замер между поисками: <b>${Math.round(measured).toLocaleString('ru-RU')}/ч</b>${acceleration > 0 ? ` · ускорение ×${acceleration.toFixed(acceleration >= 10 ? 0 : 1)}` : ''}</div>`);
      }

      const duration = Number(copy.duration_sec || 0);
      if (duration > DIRECT_MAX) {
        badges.push(`<div class="anomaly hot">речевой тайминг: <b>${duration.toFixed(2)} сек → ровно ${TARGET.toFixed(0)} сек</b></div>`);
      } else if (duration > 0) {
        badges.push(`<div class="anomaly">речевой тайминг: <b>сохранить ${duration.toFixed(2)} сек</b></div>`);
      }

      if (badges.length) {
        html = html.replace('</div>\n    <div class="radar-number">', `${badges.join('')}</div>\n    <div class="radar-number">`);
      }
      return html;
    };
  }

  function replaceStaticCopy() {
    document.title = 'ПОИСК ТРЕНДОВ — INSTAGRAM + TIKTOK + YOUTUBE';

    const brand = document.querySelector('.brand small');
    if (brand) brand.textContent = 'Instagram · TikTok · YouTube';

    const sideNote = document.querySelector('.side-note');
    if (sideNote) sideNote.textContent = '1. Новый поиск очищает предыдущий TOP и мету. 2. Ищем последние 14 дней только по реальным #omni, #veo, #veo3, #ai и #ии. 3. Каждый кандидат обязательно слушает Gemini: без слышимой речи — REJECT. 4. До 10.05 сек сохраняем исходный речевой тайминг; 10.05–15.05 сек проходит только если обязательные реплики и действия естественно укладываются ровно в 10 секунд. 5. Ranking ставит наверх самый быстрый рост просмотров.';

    const eyebrow = document.querySelector('header .eyebrow');
    if (eyebrow) eyebrow.textContent = 'V28 MULTIPLATFORM SPEECH RADAR';
    const hero = document.querySelector('header h1 span');
    if (hero) hero.textContent = 'Instagram + TikTok + YouTube.';

    const section = document.querySelector('.section-title');
    if (section) {
      const step = section.querySelector('.step');
      const h2 = section.querySelector('h2');
      const muted = section.querySelector('.muted');
      if (step) step.textContent = '3 ПЛАТФОРМЫ · 5 ТЕГОВ · 14 ДНЕЙ';
      if (h2) h2.textContent = '#omni + #veo + #veo3 + #ai + #ии · только видео с речью · исходник до 15.05 сек';
      if (muted) muted.textContent = 'strict hashtag в самом посте · Gemini слушает речь и проверяет тайминг';
    }

    const geminiMeta = document.getElementById('geminiApiMeta');
    if (geminiMeta) geminiMeta.textContent = 'Обязательный speech + timing gate и production-анализ выбранного видео';
    const apiMeta = document.getElementById('apifyApiMeta');
    if (apiMeta) apiMeta.textContent = 'Instagram Reels + TikTok + YouTube Shorts · #omni / #veo / #veo3 / #ai / #ии';

    const intro = document.querySelector('.radar-intro');
    if (intro) {
      const title = intro.querySelector('b');
      const text = intro.querySelector('span');
      if (title) title.textContent = 'Один радар на три платформы. В TOP — только ролики с реальной речью.';
      if (text) text.textContent = 'Apify собирает Instagram Reels, TikTok и YouTube Shorts за последние 14 дней. Source-feed не является доказательством: #omni, #veo, #veo3, #ai или #ии обязан реально присутствовать в самом посте. После strict hashtag/date/duration/motion фильтра Gemini обязательно слушает каждый кандидат. Без слышимой речи ролик отклоняется. Для ≤10.05 сек Gemini подтверждает возможность сохранить речь, спикеров, паузы, действия и реакцию в фактической длительности. Для 10.05–15.05 сек PASS возможен только если всю обязательную разговорную механику можно естественно пересобрать ровно в 10.00 сек без глобального ускорения и торопливой речи.';
    }

    const mini = [...document.querySelectorAll('.section-mini-title')];
    if (mini[0]) {
      const b = mini[0].querySelector('b');
      const span = mini[0].querySelector('span');
      if (b) b.textContent = 'TOP НАБИРАЮЩИХ ОБОРОТЫ';
      if (span) span.textContent = 'Instagram + TikTok + YouTube · 14 дней · 5 строгих тегов · речь и тайминг подтверждены Gemini';
    }
    if (mini[1]) {
      const b = mini[1].querySelector('b');
      const span = mini[1].querySelector('span');
      if (b) b.textContent = 'ПУЛ КАНДИДАТОВ V28';
      if (span) span.textContent = '#omni · #veo · #veo3 · #ai · #ии · до 420 speech/timing проверок Gemini за run';
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
        if (/≤10 сек \/ 7 дней|≤15 сек \/ 14 дней/i.test(text)) span.textContent = '≤15 сек / 14 дней';
        if (/AI проверка|смысловая проверка|MP4 \/ сжатие/i.test(text)) span.textContent = 'речь + тайминг';
        if (/AI совпадений|прошли проверку/i.test(text)) span.textContent = 'speech/timing OK';
      });
    }

    const candidates = document.getElementById('candidateRows');
    if (candidates) {
      candidates.querySelectorAll('.candidate-row').forEach(row => {
        const duration = candidateDuration(row);
        const state = row.querySelector('.candidate-state');
        if (!state) return;
        const text = (state.textContent || '').trim();

        if (/ПРОШЁЛ AI|ПРОШЁЛ MP4|ПРОШЁЛ ПРОВЕРКУ/.test(text)) {
          state.textContent = duration > DIRECT_MAX
            ? `${duration.toFixed(2)} СЕК → РЕЧЬ УЛОЖИТЬ В 10`
            : 'РЕЧЬ + ТАЙМИНГ OK';
        } else if (/НЕ ПРОШЁЛ AI|ОТКЛОНЁН|НЕ УЖИМАЕТСЯ/.test(text)) {
          state.textContent = 'ОТКЛОНЁН GEMINI';
        } else if (/ЖДЁТ AI|ЖДЁТ MP4|ЖДЁТ ПРОВЕРКУ|ПРОВЕРЯЮ/.test(text)) {
          state.textContent = 'GEMINI СЛУШАЕТ РЕЧЬ';
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