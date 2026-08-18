(() => {
  function capcutOverlayText(plan = {}) {
    const steps = Array.isArray(plan.steps) ? plan.steps : [];
    const head = [
      `Режим генерации\n${plan.generation_mode || 'CLEAN PLATE ONLY — наложения добавляются после генерации'}`,
      `Правило clean plate\n${plan.clean_plate_rule || 'Генерировать только исходную сцену без монтажных наложений'}`,
    ];

    if (!plan.has_editorial_overlays || !steps.length) {
      head.push('Наложения\nВ исходнике не обнаружены монтажные фото скрины PIP карточки или другие screen-space слои. Дополнительный монтаж не требуется.');
      return head.join('\n\n');
    }

    head.push(`Наложения\nОбнаружено ${steps.length}. НЕ генерировать их в PHOTO/VIDEO PROMPT — добавить вручную в CapCut после готового базового ролика.`);
    steps.forEach((s, i) => {
      head.push([
        `СЛОЙ ${i + 1} · ${s.layer_id || ''} · ${s.overlay_kind || ''}`,
        `Тайминг: ${Number(s.start_sec || 0).toFixed(2)}–${Number(s.end_sec || 0).toFixed(2)} сек`,
        `Ассет: ${s.asset_to_use || ''}`,
        `Подготовка: ${s.asset_preparation || ''}`,
        `Позиция: ${s.anchor_and_position || ''}`,
        `Экранный box: ${s.screen_box_percent || ''}`,
        `Crop / aspect: ${s.crop_and_aspect_ratio || ''}`,
        `Прозрачность / blend: ${s.opacity_and_blend || ''}`,
        `Рамка / скругление / тень: ${s.border_corner_radius_shadow || ''}`,
        `Появление: ${s.animation_in || ''}`,
        `Исчезновение: ${s.animation_out || ''}`,
        `Tracking: ${s.motion_or_tracking || ''}`,
        `Слои / перекрытие: ${s.layering_and_occlusion || ''}`,
        `CapCut: ${s.capcut_action || ''}`,
      ].join('\n'));
    });

    if (Array.isArray(plan.manual_finish_order) && plan.manual_finish_order.length) {
      head.push(`Порядок финального монтажа\n${plan.manual_finish_order.join('\n')}`);
    }
    return head.join('\n\n');
  }

  const originalBuildWholePackage = window.buildWholePackage;
  if (typeof originalBuildWholePackage === 'function') {
    window.buildWholePackage = function(d) {
      const base = originalBuildWholePackage(d);
      return `${base}\n\n════════════════════════════════\n\nCAPCUT OVERLAY PLAN\n${capcutOverlayText(d.capcut_overlay_plan || {})}`;
    };
  }

  const originalRenderResult = window.renderResult;
  if (typeof originalRenderResult === 'function') {
    window.renderResult = function(d, modelName = '') {
      originalRenderResult(d, modelName);
      const result = document.querySelector('#analysisResult');
      if (!result || typeof window.blockCard !== 'function') return;
      const plan = d.capcut_overlay_plan || {};
      const text = capcutOverlayText(plan);
      const holder = document.createElement('div');
      holder.innerHTML = window.blockCard(
        'МОНТАЖ',
        plan.has_editorial_overlays ? 'CAPCUT OVERLAY PLAN — добавить после AI' : 'CAPCUT OVERLAY PLAN — clean plate',
        text
      );
      const node = holder.firstElementChild;
      if (node) {
        result.appendChild(node);
        if (typeof window.bindCopyButtons === 'function') window.bindCopyButtons(node);
      }
    };
  }

  const replacements = [
    ['AI проверка', 'Gemini проверка'],
    ['AI совпадений', 'подходящих сценок'],
    ['ПРОШЁЛ AI', 'ПОДХОДИТ'],
    ['НЕ ПРОШЁЛ AI', 'НЕ ПОДХОДИТ'],
    ['ЖДЁТ AI', 'ЖДЁТ GEMINI'],
    ['AI-комедийная сценка', 'смешная короткая сценка'],
  ];

  function relabel(root) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      let text = node.nodeValue || '';
      let next = text;
      for (const [from, to] of replacements) next = next.split(from).join(to);
      if (next !== text) node.nodeValue = next;
    }
  }

  function relabelAll() {
    relabel(document.querySelector('#radarPipelineStats'));
    relabel(document.querySelector('#candidateRows'));
    relabel(document.querySelector('#radarRows'));
  }

  const observer = new MutationObserver(relabelAll);
  observer.observe(document.body, {childList: true, subtree: true, characterData: true});
  relabelAll();
})();
