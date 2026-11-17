(function (root) {
  'use strict';
  const inRange = age => Number.isInteger(age) && age >= 6 && age <= 13;
  const enabled = cell => !!cell?.querySelector('[data-age-seven-fourteen]')?.checked;
  const accept = (cell, key) => !enabled(cell) || inRange(DATA.meta[key]?.age);
  root.StageAgeRange = { inRange, accept };
  if (typeof module !== 'undefined') module.exports = { inRange };
  if (typeof document === 'undefined') return;
  const cards = document.getElementById('productCards');
  if (!cards) return;
  const style = document.createElement('style');
  style.textContent = '#productCards .stage-cell tr.stage-age-excluded{display:none!important}';
  document.head.append(style);
  function update(cell) {
    const active = enabled(cell);
    const inputs = [...cell.querySelectorAll('[data-stage-key]')];
    for (const input of inputs) {
      const keep = accept(cell, input.dataset.stageKey);
      input.closest('tr').hidden = !keep;
      input.closest('tr').classList.toggle('stage-age-excluded', !keep);
      if (!keep) input.checked = false;
    }
    const visible = inputs.filter(input => !input.closest('tr').hidden);
    const selected = visible.filter(input => input.checked);
    const all = cell.querySelector('[data-stage-all]');
    if (all) {
      all.checked = visible.length > 0 && selected.length === visible.length;
      all.indeterminate = selected.length > 0 && selected.length < visible.length;
      all.disabled = visible.length === 0;
    }
    cell.querySelector('[data-age-range-count]').textContent = active
      ? `上架第7—14天：${visible.length}部；复制／确认仅处理其中已勾选剧目`
      : '';
  }
  function invalidate(cell) {
    cell.querySelector('.stage-quick-copy select')?.dispatchEvent(new Event('change', { bubbles: true }));
    const area = cell.querySelector('.quick-fallback');
    if (area) { area.value = ''; area.hidden = true; }
    cell.querySelector('.quick-groups')?.replaceChildren();
    const status = cell.querySelector('.quick-status');
    if (status) status.textContent = '筛选已变更，请重新复制／确认素材';
  }
  function add() {
    for (const cell of cards.querySelectorAll('.stage-cell')) {
      const actions = cell.querySelector('.stage-quick-copy [data-action="materials"]')?.parentElement;
      if (!actions || cell.querySelector('[data-age-seven-fourteen]')) continue;
      const label = document.createElement('label');
      label.className = 'stage-age-range';
      label.style = 'display:inline-flex;align-items:center;gap:4px;padding:6px;border:1px solid #cfdeed;border-radius:6px;background:white;font-size:12px;cursor:pointer';
      label.title = '上架当天为第1天；按报告截止日北京时间计算，日期差6—13天（含边界）。在本格原阶段内进一步筛选，不扩大范围。';
      label.innerHTML = '<input type="checkbox" data-age-seven-fourteen style="width:14px;height:14px;margin:0">上架7—14天';
      actions.append(label);
      const count = document.createElement('small');
      count.dataset.ageRangeCount = '';
      count.style = 'display:block;color:#637d9b;margin-top:6px';
      actions.after(count);
      label.querySelector('input').onchange = () => { update(cell); invalidate(cell); };
    }
  }
  cards.addEventListener('change', event => {
    if (!event.target.matches('[data-stage-key],[data-stage-all]')) return;
    const cell = event.target.closest('.stage-cell');
    if (cell?.querySelector('[data-age-seven-fourteen]')) update(cell);
  });
  new MutationObserver(add).observe(cards, { childList: true, subtree: true });
  add();
})(typeof window === 'undefined' ? globalThis : window);
