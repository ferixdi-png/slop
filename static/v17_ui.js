(() => {
  const patchRadarLabels = () => {
    const stage = document.querySelector('#radarStage');
    if (stage && /Gemini\s+проверяет/i.test(stage.textContent || '')) {
      stage.textContent = 'Проверяю диалог и шутку';
    }

    document.querySelectorAll('#radarPipelineStats span').forEach((el) => {
      const text = String(el.textContent || '').trim();
      if (text === 'AI проверка') el.textContent = 'смысловая проверка';
      if (text === 'AI совпадений') el.textContent = 'подходящих сценок';
    });
  };

  const observer = new MutationObserver(patchRadarLabels);
  const start = () => {
    patchRadarLabels();
    observer.observe(document.body, {subtree: true, childList: true, characterData: true});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
