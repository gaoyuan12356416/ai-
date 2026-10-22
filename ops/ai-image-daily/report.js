'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const arr = value => Array.isArray(value) ? value : [];
  const R = JSON.parse($('report-data').textContent);
  // Local render_report previews can still display old archived reports.
  if (!R.views) R.views = {all: {...R, id: 'all', name: 'DramaWave', week: {}}};
  const views = R.views;
  const sourceLabels = {cover_synthesis: '封面图合成', youtube_cover: 'YouTube 封面',
    ad_material: '其他已验证图片', new_upload: '逐图上传账本'};
  const sourceName = key => sourceLabels[key] || key || '待核对来源';
  const number = value => value === null || value === undefined || value === '' ||
    !Number.isFinite(Number(value)) ? null : Number(value);
  const fmt = (value, digits = 0) => number(value) === null ? '—' :
    new Intl.NumberFormat('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(Number(value));
  const money = value => fmt(value, 2);
  const pct = value => number(value) === null ? '—' : fmt(value, 2) + '%';
  const dateOnly = value => String(value || '').slice(0, 10) || '—';
  const timeText = value => value ? String(value).replace('T', ' ').replace(/\+08:00$/, ' +08') : '未提供';
  function node(tag, text, className) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (className) n.className = className;
    return n;
  }
  function safeUrl(value) {
    try {
      if (typeof value !== 'string' || /[\u0000-\u001f]/.test(value)) return '';
      const u = new URL(value);
      return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : '';
    } catch (_) { return ''; }
  }
  const td = (text, cls = '') => node('td', text, cls);
  function link(url, text, cls) {
    const a = node('a', text, cls), safe = safeUrl(url);
    if (safe) { a.href = safe; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    return a;
  }
  function kpi(root, label, value, note, primary = false) {
    const c = node('div', null, 'kpi' + (primary ? ' primary' : ''));
    c.append(node('div', label, 'kpi-label'), node('div', value, 'kpi-value'), node('div', note, 'kpi-note'));
    root.append(c);
  }
  function ratioDelta(current, previous, kind = 'relative') {
    const a = number(current), b = number(previous);
    if (a === null || b === null || (!b && kind === 'relative')) return '前日对比 —';
    const d = kind === 'points' ? a - b : (a - b) / Math.abs(b) * 100;
    return '较前日 ' + (d > 0 ? '+' : '') + fmt(d, kind === 'points' ? 2 : 1) + (kind === 'points' ? ' 个百分点' : '%');
  }
  let selected = 'all', period = 'day', chartKey = 'roas', activeGroup = 'sources';
  let expanded = false, page = 1, pageSize = 25, sortKey = 'spend', sortDirection = -1;
  let detailsRequested = false;
  const detailCache = new Map(), pending = new Map(), detailErrors = new Map();
  for (const [id, view] of Object.entries(views)) {
    if (Array.isArray(view.materials)) detailCache.set(id, {materials: view.materials,
      groups: view.groups || {}, week_groups: (view.week || {}).groups || {}, unresolved: view.unresolved || []});
  }
  const view = () => views[selected] || views.all;
  const selectedSummary = v => period === 'week' ? v.week || {} : v.summary || {};
  const selectedComparisons = () => period === 'week' ? (view().week || {}).comparisons || {} : view().comparisons || {};
  const selectedDetails = () => detailCache.get(selected);
  const metricRow = row => period === 'week' ? {...row, ...(row.week || {})} : row;
  function periodBounds() {
    const trend = arr(view().trend);
    return period === 'week' ? [trend[0]?.dt || R.period?.start || R.report_date, R.report_date] : [R.report_date, R.report_date];
  }
  function periodText() { const [start, end] = periodBounds(); return start === end ? end : start + ' — ' + end; }
  const productIds = ['all', ...arr(R.products).map(p => String(p.id)).filter(id => id !== 'all')];
  if (productIds.length === 1) productIds.push(...Object.keys(views).filter(id => id !== 'all'));
  const buttons = new Map();
  for (const id of [...new Set(productIds)].filter(id => views[id])) {
    const b = node('button', id === 'all' ? '三产品合计' : views[id].name);
    b.type = 'button'; b.dataset.product = id; b.addEventListener('click', () => selectProduct(id));
    buttons.set(id, b); $('product-controls').append(b);
  }
  const home = safeUrl(R.home_url);
  $('home-link').href = home || '#';
  const syncLatestLink = () => { $('latest-link').href = home + (location.hash || ''); };
  syncLatestLink();
  $('report-date').textContent = R.report_date || '—'; $('currency-label').textContent = R.currency || 'USD';
  document.title = (R.report_date || '') + ' · 三产品 AI 图片素材日报';
  function addMeta(label, value) {
    const n = node('span', label + ' '); n.append(node('strong', value)); $('meta-row').append(n);
  }
  addMeta('生成', timeText(R.generated_at)); addMeta('时区', 'Asia/Shanghai');
  addMeta('统计口径', '后台 dt · USD · AF D0'); addMeta('版本', 'v' + (R.schema_version || 2));
  const validation = R.validation || {};
  $('validation-badge').textContent = validation.status === 'passed' ? '数据校验通过' : '数据校验待确认';
  $('validation-detail').textContent = '数据校验：' + (validation.status === 'passed' ? '已通过' : '待确认') +
    ' · ' + fmt(validation.checks) + ' 项检查。待核对记录不计入正式汇总。';
  for (const note of arr(R.notes)) $('extra-notes').append(node('p', note));
  for (const p of arr(R.products)) {
    const apps = arr(p.apps).map(app => typeof app === 'object' ?
      String(app.id ?? app.app_id ?? '') + (app.name || app.app_name ? ' ' + (app.name || app.app_name) : '') : String(app));
    $('app-scope').append(node('p', p.name + '（' + p.id + '）：' + (apps.join('；') || '见本次产品范围清单')));
  }
  const historical = /\/daily\/\d{4}-\d{2}-\d{2}\//.test(location.pathname || '');
  if (historical || R.snapshot_kind === 'initial') {
    $('snapshot-notice').hidden = false;
    $('snapshot-notice').textContent = historical ?
      '正在查看历史快照：' + R.report_date + '。本页保留当次生成结果；查看最新数据请使用上方固定入口。' :
      '首次三产品快照。近 7 天投放按本次读取结果汇总，历史页保留当次生成的数据。';
  }
  let latestStatus = null;
  function checkFreshness() {
    const notice = $('stale-notice');
    if (latestStatus?.state === 'failed') {
      notice.hidden = false;
      notice.textContent = '最新日报更新失败（尝试数据日 ' + (latestStatus.attempted_date || '待确认') + '）。' +
        (historical ? '当前历史快照仍保留原始生成结果；请通过上方「查看最新日报」查看更新状态。' :
          '当前继续展示上一份成功报告（' + R.report_date + '），缺失数据未按零消耗处理。');
      return;
    }
    if (historical) return;
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
      .formatToParts(new Date()).filter(x => x.type !== 'literal').map(x => [x.type, x.value]));
    const afterRetries = +parts.hour * 60 + +parts.minute >= 9 * 60 + 35;
    const afterStart = +parts.hour >= 9;
    const yesterday = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day - 1)).toISOString().slice(0, 10);
    const expected = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day - (afterRetries ? 1 : 2))).toISOString().slice(0, 10);
    if (R.report_date && R.report_date < expected) {
      notice.hidden = false;
      notice.textContent = '数据未更新 / 等待恢复。最新应更新至 ' + expected +
        '，当前保留上一份成功报告（' + R.report_date + '）。未将缺失数据当作零消耗。';
    } else if (afterStart && !afterRetries && R.report_date < yesterday) {
      notice.hidden = false;
      notice.textContent = '今日日报正在生成或等待补偿重试（目标数据日 ' + yesterday + '）。当前显示上一份成功报告；每日 09:00 开始，09:10、09:30 自动补偿。';
    } else if (latestStatus?.report_date > R.report_date) {
      notice.hidden = false;
      notice.textContent = '已有更新数据日 ' + latestStatus.report_date + '。请点击上方「查看最新日报」刷新至最新报告。';
    } else {
      notice.hidden = true;
    }
  }
  checkFreshness(); setInterval(checkFreshness, 60000);
  let historyLoaded = false;
  function populateHistory(items) {
    const select = $('history-select'); select.replaceChildren(node('option', '查看历史日报'));
    select.children[0].value = '';
    for (const item of arr(items).slice().sort((a, b) => String(b.date).localeCompare(String(a.date)))) {
      const url = safeUrl(item.url); if (!url) continue;
      const opt = node('option', item.date + (item.date === R.report_date ? ' · 当前数据日' : ''));
      opt.value = url; select.append(opt);
    }
  }
  async function fetchJson(url, mutable = false) {
    const safe = safeUrl(url);
    if (!safe || (R.home_url && new URL(safe).origin !== new URL(R.home_url).origin)) throw new Error('资源地址无效');
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(safe, {cache: mutable ? 'no-cache' : 'default', signal: controller.signal});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  async function loadHistory() {
    if (historyLoaded || !R.history_url) return;
    historyLoaded = true;
    try { const result = await fetchJson(R.history_url, true); populateHistory(Array.isArray(result) ? result : result.entries); }
    catch (_) { historyLoaded = false; $('history-select').title = '历史目录暂时不可用，可稍后重新点击加载；最新日报入口仍可使用。'; }
  }
  async function loadStatus() {
    if (!R.status_url) return;
    try { latestStatus = await fetchJson(R.status_url, true); checkFreshness(); }
    catch (_) { $('validation-badge').title = '当前快照校验状态不变；最新更新状态暂时无法读取。'; }
  }
  populateHistory(R.history);
  $('history-select').addEventListener('focus', loadHistory); $('history-select').addEventListener('pointerdown', loadHistory);
  $('history-select').addEventListener('change', event => {
    const url = safeUrl(event.target.value); if (url) location.assign(url + (location.hash || ''));
  });
  function renderProductTable() {
    $('product-comparison-body').replaceChildren(); $('product-comparison-period').textContent = periodText();
    for (const id of productIds.filter(id => views[id])) {
      const v = views[id], s = selectedSummary(v), p = s.production || {}, m = s.performance || {};
      const tr = node('tr', null, id === selected ? 'focus-row' : '');
      const name = td(null), b = node('button', id === 'all' ? '三产品合计' : v.name, 'product-label');
      b.type = 'button'; b.addEventListener('click', () => selectProduct(id)); name.append(b);
      tr.append(name, ...[fmt(p.new_count), fmt(p.new_image_count), money(m.spend), money(m.af_revenue_d0),
        pct(m.roas), fmt(m.spending_materials), pct(m.ctr), money(m.cpi)].map(value => td(value, 'num')));
      $('product-comparison-body').append(tr);
    }
  }
  function renderSummary() {
    const s = selectedSummary(view()), prod = s.production || {}, metric = s.performance || {};
    const comparisons = selectedComparisons(), previous = comparisons.previous || {};
    $('view-context').textContent = (selected === 'all' ? '三产品合计' : view().name) + ' · ' + periodText() +
      ' · 产出按来源 / 效果按实际投放';
    $('production-title').textContent = (period === 'week' ? '近 7 天' : '昨日') + '入库';
    $('performance-title').textContent = (period === 'week' ? '近 7 天累计' : '昨日') + '投放表现';
    $('production-kpis').replaceChildren(); $('performance-kpis').replaceChildren(); $('submetrics').replaceChildren();
    kpi($('production-kpis'), '新增正式素材', fmt(prod.new_count), '按正式 ID · 含各类补传', true);
    kpi($('production-kpis'), '首次入库原图', fmt(prod.new_image_count), '来源版本去重 · 不是当日生成张数');
    kpi($('production-kpis'), '接口迁移补传', fmt(prod.migration_count), '不重复计入首次原图');
    kpi($('production-kpis'), '历史补传', fmt(prod.historical_backfill_count ?? prod.backfill_count), '迁移以外的历史图片补传');
    kpi($('production-kpis'), '新增覆盖剧目', fmt(prod.dramas), '按产品与剧目 ID 去重');
    kpi($('production-kpis'), '累计正式素材', fmt(prod.total_asof), '截至 ' + R.report_date + ' · 含已删除');
    const delta = (key, kind) => period === 'week' ? '近 7 个完整统计日累计' : ratioDelta(metric[key], previous[key], kind);
    kpi($('performance-kpis'), '消耗 · USD', money(metric.spend), delta('spend'));
    kpi($('performance-kpis'), 'AF D0 收入 · USD', money(metric.af_revenue_d0), delta('af_revenue_d0'));
    kpi($('performance-kpis'), 'AF D0 ROAS', pct(metric.roas), delta('roas', 'points'));
    kpi($('performance-kpis'), '有消耗素材', fmt(metric.spending_materials), '正式 ID 去重 · 跨产品不重复相加');
    kpi($('performance-kpis'), '每次安装成本 CPI', money(metric.cpi), delta('cpi'));
    for (const [label, value] of [['曝光', fmt(metric.impressions)], ['点击', fmt(metric.clicks)], ['安装', fmt(metric.installs)],
      ['CTR', pct(metric.ctr)], ['AF 安装', fmt(metric.af_installs)], ['付费', fmt(metric.purchases)]]) {
      const n = node('span', label); n.append(node('strong', value)); $('submetrics').append(n);
    }
    $('comparison-body').replaceChildren();
    const rows = [['AI 图片 · ' + (period === 'week' ? '近 7 天' : '昨日'), metric, true]];
    if (period === 'day') rows.push(['AI 图片 · 前一日', previous, false]);
    rows.push([selected === 'all' ? '三产品大盘' : view().name + ' 大盘', comparisons.market || {}, false],
      ['同应用同渠道其他图片', comparisons.other_images || {}, false]);
    for (const [title, data, active] of rows) {
      const tr = node('tr', null, active ? 'focus-row' : '');
      tr.append(td(title), ...[money(data.spend), money(data.af_revenue_d0), pct(data.roas), money(data.cpi)].map(v => td(v, 'num')));
      $('comparison-body').append(tr);
    }
    $('insights').replaceChildren();
    const insights = arr(period === 'week' ? view().week?.insights : view().insights);
    for (const text of insights) $('insights').append(node('div', text, 'insight'));
    if (!insights.length) $('insights').append(node('div', '以上为当前数据范围的实际表现；投放与产出按各自归属统计。', 'insight'));
    renderProductTable(); renderTop(); renderChart();
    document.documentElement.dataset.summaryReady = 'true';
  }
  function renderTop() {
    const compact = period === 'week' ? view().week_top : view().top;
    const data = compact || {dramas: arr((period === 'week' ? view().week?.groups : view().groups)?.dramas).slice()
      .sort((a, b) => (b.spend || 0) - (a.spend || 0)).slice(0, 10),
      materials: arr(view().materials).map(metricRow).filter(m => m.spend > 0).sort((a, b) => b.spend - a.spend).slice(0, 10)};
    for (const [kind, target] of [['dramas', 'top-dramas-body'], ['materials', 'top-materials-body']]) {
      $(target).replaceChildren();
      for (const row of arr(data[kind])) {
        const tr = node('tr'), name = td(null);
        if (kind === 'materials') {
          const b = node('button', row.name || 'ID ' + row.id, 'product-label');
          b.title = '查看素材 ' + row.id + ' 明细';
          b.addEventListener('click', async () => { $('search').value = String(row.id); page = 1; await loadDetails();
            $('materials').scrollIntoView({behavior: 'smooth'}); renderMaterials(); });
          name.append(b, node('small', ' · ' + (row.source_product_name || '—'), 'muted'));
        } else name.textContent = row.name || '未映射剧目';
        tr.append(name, td(money(row.spend), 'num'), td(money(row.af_revenue_d0), 'num'), td(pct(row.roas), 'num'));
        $(target).append(tr);
      }
      if (!arr(data[kind]).length) { const cell = td('所选范围暂无有消耗记录', 'empty-state'); cell.colSpan = 4; const tr = node('tr'); tr.append(cell); $(target).append(tr); }
    }
  }
  const svgNS = 'http://www.w3.org/2000/svg';
  function svgNode(tag, attrs, text) {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    if (text !== undefined) n.textContent = String(text); return n;
  }
  function renderChart() {
    const trend = arr(view().trend).slice().sort((a, b) => String(a.dt).localeCompare(String(b.dt))), key = chartKey;
    $('chart').replaceChildren(); $('chart-legend').replaceChildren();
    $('trend-period').textContent = trend.length ? trend[0].dt + ' — ' + trend[trend.length - 1].dt + ' · 本次读取后的最新值' : '暂无趋势数据';
    if (!trend.length) { $('chart').append(node('div', '暂无趋势数据', 'empty-state')); return; }
    const series = key === 'uploads' ? [{key: 'uploads', name: '正式入库', color: '#087f87'},
      {key: 'new_image_count', name: '首次原图', color: '#7694c5'}, {key: 'migration_count', name: '接口迁移', color: '#b38c55'}] :
      [{key: 'ai', name: 'AI 图片', color: '#087f87'}, {key: 'market', name: '所选产品大盘', color: '#a0adb8'},
        {key: 'other_images', name: '其他图片（同应用同渠道）', color: '#7694c5'}];
    const W = 680, H = 238, L = 58, RR = 20, T = 18, B = 40, iw = W - L - RR, ih = H - T - B;
    const value = (row, s) => number(key === 'uploads' ? row[s.key] : (row[s.key] || {})[key]);
    const values = series.flatMap(s => trend.map(t => value(t, s))).filter(v => v !== null);
    let max = Math.max(0, ...values), min = Math.min(0, ...values); if (max === min) { max = 1; min = 0; }
    max += (max - min) * .12;
    const y = v => T + ih * (1 - (v - min) / (max - min)), x = i => L + iw * (trend.length === 1 ? .5 : i / (trend.length - 1));
    const svg = svgNode('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '近 7 天 ' + key + ' 趋势'});
    for (let i = 0; i <= 4; i++) {
      const n = min + (max - min) * i / 4, yp = y(n);
      svg.append(svgNode('line', {x1: L, y1: yp, x2: W - RR, y2: yp, stroke: '#e9eff3', 'stroke-dasharray': '3 5'}));
      const label = key === 'roas' ? fmt(n) + '%' : Math.abs(n) >= 10000 ? fmt(n / 1000, 1) + 'k' : fmt(n, key === 'uploads' ? 0 : Math.abs(n) < 10 ? 1 : 0);
      svg.append(svgNode('text', {x: L - 10, y: yp + 4, 'text-anchor': 'end', fill: '#8a9ba9', 'font-size': 11}, label));
    }
    trend.forEach((t, i) => svg.append(svgNode('text', {x: x(i), y: H - 14, 'text-anchor': 'middle', fill: '#8a9ba9', 'font-size': 11}, String(t.dt).slice(5))));
    for (const s of series) {
      let path = '', open = false;
      trend.forEach((t, i) => { const v = value(t, s); if (v === null) { open = false; return; }
        path += (open ? 'L' : 'M') + x(i) + ',' + y(v) + ' '; open = true; });
      svg.append(svgNode('path', {d: path, fill: 'none', stroke: s.color, 'stroke-width': s.key === 'ai' || s.key === 'uploads' ? 3 : 1.7}));
      trend.forEach((t, i) => { const v = value(t, s); if (v === null) return;
        const dot = svgNode('circle', {cx: x(i), cy: y(v), r: 3.5, fill: s.color, stroke: 'white', 'stroke-width': 1.5});
        dot.append(svgNode('title', {}, t.dt + ' · ' + s.name + '：' + (key === 'roas' ? pct(v) : key === 'spend' ? money(v) : fmt(v)))); svg.append(dot); });
      const legend = node('span'), dot = node('i', null, 'legend-dot'); dot.style.backgroundColor = s.color;
      legend.append(dot, document.createTextNode(s.name)); $('chart-legend').append(legend);
    }
    $('chart').append(svg);
  }
  const groupNames = {sources: ['素材来源', '来源'], owners: ['素材负责人', '负责人'], ratios: ['图片尺寸', '尺寸'],
    dramas: ['剧目表现', '剧目'], optimizers: ['投放优化师', '优化师'], languages: ['素材语言', '语言']};
  function emptyRow(target, text, span) { const tr = node('tr'), cell = td(text, 'empty-state'); cell.colSpan = span; tr.append(cell); $(target).replaceChildren(tr); }
  function renderGroups() {
    $('group-heading').textContent = groupNames[activeGroup][0]; $('group-name-heading').textContent = groupNames[activeGroup][1];
    $('group-desc').textContent = periodText() + ' · 产出按来源归属；投放按实际应用归属。' +
      (activeGroup === 'optimizers' ? '优化师覆盖素材可重复，素材数不相加。' : '素材数按正式 ID，首次原图按来源版本去重。');
    const details = selectedDetails();
    if (!details) { emptyRow('group-body', detailErrors.has(selected) ? '明细加载失败，请点击重试。' : '查看本区域时加载分组数据。', 11); $('group-count').textContent = ''; $('group-expand').hidden = true; return; }
    const groups = period === 'week' ? details.week_groups : details.groups;
    const rows = arr(groups?.[activeGroup]).slice().sort((a, b) => (b.spend || 0) - (a.spend || 0) || (b.new_count || 0) - (a.new_count || 0));
    $('group-body').replaceChildren();
    const max = Math.max(0, ...rows.map(r => r.spend || 0));
    for (const g of expanded ? rows : rows.slice(0, 12)) {
      const tr = node('tr'), first = td(null);
      first.append(node('span', activeGroup === 'sources' ? sourceName(g.name) : g.name || '未标记', 'group-name'));
      if (max > 0) { const track = node('div', null, 'bar-track'), bar = node('div', null, 'bar-fill');
        bar.style.width = Math.max(0, (g.spend || 0) / max * 100) + '%'; track.append(bar); first.append(track); }
      const production = key => activeGroup === 'optimizers' ? '—' : fmt(g[key]);
      tr.append(first, ...[fmt(g.count), production('new_count'), production('backfill_count'), production('new_image_count'),
        production('migration_count'), money(g.spend), money(g.af_revenue_d0), pct(g.roas), pct(g.ctr), money(g.cpi)].map(v => td(v, 'num')));
      $('group-body').append(tr);
    }
    if (!rows.length) emptyRow('group-body', '暂无该维度数据', 11);
    $('group-count').textContent = '共 ' + rows.length + ' 组 · 按消耗优先排序' + (!expanded && rows.length > 12 ? ' · 展示前 12 组' : '');
    $('group-expand').hidden = rows.length <= 12; $('group-expand').textContent = expanded ? '收起' : '展开全部';
  }
  const filterDefs = [['source-filter', 'source', '全部来源', sourceName], ['owner-filter', 'owner', '全部负责人'],
    ['ratio-filter', 'ratio', '全部尺寸'], ['language-filter', 'language', '全部语言']];
  function fillFilters() {
    const materials = arr(selectedDetails()?.materials);
    for (const [id, key, title, translate] of filterDefs) {
      const select = $(id), previous = select.value; select.replaceChildren();
      const blank = node('option', title); blank.value = ''; select.append(blank);
      const values = [...new Set(materials.map(m => String(m[key] || '')))].filter(Boolean).sort((a, b) => a.localeCompare(b, 'zh-CN'));
      for (const value of values) { const opt = node('option', translate ? translate(value) : value); opt.value = value; select.append(opt); }
      select.value = values.includes(previous) ? previous : '';
    }
  }
  function matching() {
    const q = $('search').value.trim().toLocaleLowerCase(), [start, end] = periodBounds();
    return arr(selectedDetails()?.materials).map(metricRow).filter(m => {
      if (q && !['id', 'name', 'content_id', 'owner', 'language', 'source_product_name'].map(k => m[k] || '')
        .concat(arr(m.optimizers), arr(m.delivery_product_names)).join(' ').toLocaleLowerCase().includes(q)) return false;
      for (const [id, key] of filterDefs) if ($(id).value && String(m[key] || '') !== $(id).value) return false;
      if ($('new-only').checked && (dateOnly(m.created_at) < start || dateOnly(m.created_at) > end || m.is_source_product === false)) return false;
      if ($('spend-only').checked && !(number(m.spend) > 0)) return false;
      if ($('backfill-only').checked && !m.is_backfill) return false;
      if ($('deleted-only').checked && !Number(m.is_delete)) return false;
      return true;
    }).sort((a, b) => {
      const av = sortKey === 'created_at' ? String(a.created_at || '') : number(a[sortKey]);
      const bv = sortKey === 'created_at' ? String(b.created_at || '') : number(b[sortKey]);
      if (av === null && bv !== null) return 1; if (bv === null && av !== null) return -1;
      return (typeof av === 'string' ? av.localeCompare(bv) : (av || 0) - (bv || 0)) * sortDirection || String(a.id).localeCompare(String(b.id));
    });
  }
  function thumbnail(m) {
    const original = safeUrl(m.url), thumbnailUrl = safeUrl(m.thumbnail_url);
    const root = original ? link(original, null, 'thumb-link') : node('div');
    if (original) root.setAttribute('aria-label', '查看素材 ' + m.id + ' 原图');
    // Never use the original URL as an image fallback: it can be multi-megabyte.
    if (thumbnailUrl && thumbnailUrl !== original) {
      const img = node('img', null, 'thumb'); img.src = thumbnailUrl; img.alt = m.name || '素材 ' + m.id;
      img.loading = 'lazy'; img.decoding = 'async'; img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => root.replaceChildren(node('div', original ? '查看原图 ↗' : '缩略图不可用', 'thumb-placeholder')), {once: true});
      root.append(img);
    } else root.append(node('div', original ? '查看原图 ↗' : '无图片链接', 'thumb-placeholder'));
    return root;
  }
  function renderMaterials() {
    const details = selectedDetails(); $('export').disabled = !details;
    if (!details) {
      emptyRow('material-body', detailErrors.has(selected) ? '明细暂时不可用，汇总仍为已校验的快照；点击上方按钮重试。' : '完整明细按当前产品加载。', 14);
      $('material-summary').textContent = '已确认 ' + fmt(view().material_count) + ' 张 · 每个正式素材 ID 一行';
      $('page-info').textContent = ''; $('page-number').textContent = '—'; $('filter-totals').textContent = '';
      $('prev-page').disabled = true; $('next-page').disabled = true; return;
    }
    const filtered = matching(), pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.min(Math.max(1, page), pages); $('material-body').replaceChildren();
    const shown = filtered.slice((page - 1) * pageSize, page * pageSize);
    for (const m of shown) {
      const tr = node('tr'), first = td(null), info = node('div', null, 'material-info'); info.append(thumbnail(m));
      const text = node('div'), name = node('div', m.name || '未命名素材', 'material-name'); name.title = m.name || '';
      text.append(name, node('div', 'ID ' + String(m.id || ''), 'material-id'));
      const tags = node('div', null, 'material-tags'); tags.append(node('span', sourceName(m.source), 'tag'));
      if (m.is_backfill) tags.append(node('span', m.backfill_kind === 'migration' ? '接口迁移补传' : '历史补传', 'tag backfill'));
      if (Number(m.is_delete)) tags.append(node('span', '已删除', 'tag deleted'));
      if (m.is_source_product === false) tags.append(node('span', '跨产品投放', 'tag'));
      text.append(tags); info.append(text); first.append(info);
      const products = td(m.source_product_name || '未确认', 'td-two product-cell');
      products.append(node('small', '投放：' + (arr(m.delivery_product_names).join('、') || '暂无投放')));
      const owner = td(m.owner || '未映射', 'td-two'); owner.append(node('small', m.language || '未标记语言'));
      const drama = td(m.content_id || '未映射剧目', 'td-two'); drama.append(node('small', m.ratio || '未标记尺寸'));
      const dates = td(dateOnly(m.created_at), 'td-two'); dates.append(node('small', '原图 ' + dateOnly(m.generated_at)));
      dates.title = '入库：' + timeText(m.created_at) + '\n原图：' + timeText(m.generated_at);
      const optimizer = td(arr(m.optimizers).join('、') || '—', 'product-cell');
      tr.append(first, products, owner, drama, dates, optimizer, ...[money(m.spend), money(m.af_revenue_d0), pct(m.roas),
        fmt(m.impressions), fmt(m.clicks), fmt(m.installs), pct(m.ctr), money(m.cpi)].map(v => td(v, 'num')));
      $('material-body').append(tr);
    }
    if (!shown.length) emptyRow('material-body', details.materials.length ? '没有符合当前筛选条件的素材。' : '暂无可确认的正式图片。', 14);
    $('material-summary').textContent = periodText() + ' · 共 ' + fmt(details.materials.length) + ' 张 · 每个正式素材 ID 一行';
    const sum = key => filtered.reduce((total, m) => total + (number(m[key]) || 0), 0);
    const spend = sum('spend');
    $('filter-totals').textContent = '筛选 ' + fmt(filtered.length) + ' 张 · 消耗 ' + money(spend) +
      ' · AF D0 收入 ' + money(sum('af_revenue_d0')) + ' · ROAS ' + pct(spend ? sum('af_revenue_d0') / spend * 100 : null);
    $('page-info').textContent = filtered.length ? '展示 ' + fmt((page - 1) * pageSize + 1) + '–' + fmt(Math.min(page * pageSize, filtered.length)) + ' / ' + fmt(filtered.length) + ' 张' : '暂无可展示素材';
    $('page-number').textContent = page + ' / ' + pages; $('prev-page').disabled = page <= 1; $('next-page').disabled = page >= pages;
    document.querySelectorAll('[data-sort]').forEach(b => {
      const titles = {created_at: '入库 / 原图生成', spend: '消耗', af_revenue_d0: 'AF D0 收入', roas: 'ROAS'};
      b.textContent = titles[b.dataset.sort] + ' ' + (sortKey === b.dataset.sort ? (sortDirection < 0 ? '↓' : '↑') : '↕');
      b.setAttribute('aria-pressed', String(sortKey === b.dataset.sort));
    });
  }
  function renderUnresolved() {
    const data = selectedDetails(); $('unresolved-list').replaceChildren();
    $('unresolved-count').textContent = '(' + fmt(data ? arr(data.unresolved).length : view().unresolved_count) + ')';
    if (!data) { $('unresolved-list').append(node('div', '完整明细加载后展示待核对记录。', 'empty-state')); return; }
    if (!arr(data.unresolved).length) $('unresolved-list').append(node('div', '暂无待核对记录。', 'empty-state'));
    for (const row of arr(data.unresolved)) {
      const item = node('div', row.name || row.id || '未命名记录', 'item');
      item.append(node('small', [row.id ? 'ID ' + row.id : '', row.source_product_name, sourceName(row.source), row.reason || '缺少映射证据'].filter(Boolean).join(' · ')));
      $('unresolved-list').append(item);
    }
  }
  function updateDetailStatus() {
    const loaded = detailCache.has(selected), failed = detailErrors.has(selected), loading = pending.has(selected);
    $('detail-status').classList.toggle('error', failed); $('load-details').disabled = loading;
    $('load-details').hidden = loaded; $('load-details').textContent = failed ? '重试加载' : loading ? '加载中…' : '加载完整明细';
    $('detail-message').textContent = loaded ? '已加载 ' + (selected === 'all' ? '三产品合计' : view().name) +
      ' 的分组、素材明细与待核对记录。' : failed ? '资源加载失败，汇总仍可查看；请重试。' :
      loading ? '正在加载当前产品明细…' : '完整明细将在查看时按产品加载。';
  }
  function renderDetails() { updateDetailStatus(); renderGroups(); renderMaterials(); renderUnresolved(); }
  async function loadDetails() {
    detailsRequested = true;
    const id = selected;
    if (detailCache.has(id)) { fillFilters(); renderDetails(); return; }
    if (pending.has(id)) return pending.get(id);
    detailErrors.delete(id);
    const task = (async () => {
      try {
        const data = await fetchJson(views[id].details_url);
        if (!Array.isArray(data.materials) || !data.groups || !data.week_groups || !Array.isArray(data.unresolved)) throw new Error('明细格式无效');
        detailCache.set(id, data);
      } catch (error) { detailErrors.set(id, error.message || '读取失败'); }
      finally { pending.delete(id); if (selected === id) { fillFilters(); renderDetails(); } }
    })();
    pending.set(id, task); updateDetailStatus(); return task;
  }
  function selectProduct(id) {
    if (!views[id]) return;
    selected = id; page = 1; expanded = false;
    for (const [key, b] of buttons) { b.classList.toggle('active', key === id); b.setAttribute('aria-pressed', String(key === id)); }
    // Preserve search/checkbox intent; only invalid dropdown choices are cleared.
    fillFilters(); renderSummary(); renderDetails(); if (detailsRequested) loadDetails();
  }
  document.querySelectorAll('[data-period]').forEach(b => b.addEventListener('click', () => {
    period = b.dataset.period; page = 1;
    document.querySelectorAll('[data-period]').forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); });
    renderSummary(); renderDetails();
  }));
  document.querySelectorAll('[data-chart]').forEach(b => b.addEventListener('click', () => {
    chartKey = b.dataset.chart;
    document.querySelectorAll('[data-chart]').forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); }); renderChart();
  }));
  document.querySelectorAll('[data-group]').forEach(b => b.addEventListener('click', () => {
    activeGroup = b.dataset.group; expanded = false;
    document.querySelectorAll('[data-group]').forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-selected', String(x === b)); });
    renderGroups(); loadDetails();
  }));
  $('group-expand').addEventListener('click', () => { expanded = !expanded; renderGroups(); });
  $('load-details').addEventListener('click', loadDetails);
  let searchTimer;
  $('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; renderMaterials(); loadDetails(); }, 120); });
  for (const id of [...filterDefs.map(f => f[0]), 'new-only', 'spend-only', 'backfill-only', 'deleted-only'])
    $(id).addEventListener('change', () => { page = 1; renderMaterials(); loadDetails(); });
  $('reset-filters').addEventListener('click', () => {
    $('search').value = ''; for (const [id] of filterDefs) $(id).value = '';
    for (const id of ['new-only', 'spend-only', 'backfill-only', 'deleted-only']) $(id).checked = false;
    page = 1; renderMaterials();
  });
  document.querySelectorAll('[data-sort]').forEach(b => b.addEventListener('click', () => {
    sortDirection = sortKey === b.dataset.sort ? -sortDirection : -1; sortKey = b.dataset.sort; page = 1; renderMaterials();
  }));
  $('page-size').addEventListener('change', () => { pageSize = Number($('page-size').value); page = 1; renderMaterials(); });
  $('prev-page').addEventListener('click', () => { page--; renderMaterials(); });
  $('next-page').addEventListener('click', () => { page++; renderMaterials(); });
  function csvCell(value) {
    if (value === null || value === undefined) return '""'; let text = String(value);
    if (typeof value !== 'number' && /^[\s]*[=+\-@]/.test(text)) text = "'" + text;
    if (typeof value !== 'number' && /^[\t\r\n]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  }
  $('export').addEventListener('click', () => {
    if (!selectedDetails()) return;
    const columns = [['统计开始日', () => periodBounds()[0]], ['统计结束日', () => R.report_date], ['筛选产品', () => view().name],
      ['正式素材ID', m => String(m.id || '')], ['素材名称', m => m.name], ['原图URL', m => safeUrl(m.url)],
      ['来源产品ID', m => m.source_product_id], ['来源产品', m => m.source_product_name],
      ['投放产品ID', m => arr(m.delivery_product_ids).join('、')], ['投放产品', m => arr(m.delivery_product_names).join('、')],
      ['来源', m => sourceName(m.source)], ['负责人', m => m.owner], ['剧目ID', m => m.content_id],
      ['语言', m => m.language], ['正式国家', m => m.country], ['请求国家', m => m.requested_country], ['尺寸', m => m.ratio],
      ['入库时间', m => m.created_at], ['原图生成时间', m => m.generated_at], ['补传', m => m.is_backfill ? '是' : '否'],
      ['接口迁移补传', m => m.backfill_kind === 'migration' ? '是' : '否'], ['首次入库时间', m => m.first_ingestion_at],
      ['旧正式素材ID', m => arr(m.legacy_final_ids).join('、')], ['已删除', m => Number(m.is_delete) ? '是' : '否'],
      ['投放优化师', m => arr(m.optimizers).join('、')], ['币种', () => R.currency || 'USD'], ['消耗', m => m.spend],
      ['AF D0收入', m => m.af_revenue_d0], ['ROAS(%)', m => m.roas], ['曝光', m => m.impressions], ['点击', m => m.clicks],
      ['安装', m => m.installs], ['AF安装', m => m.af_installs], ['付费', m => m.purchases], ['CTR(%)', m => m.ctr], ['CPI', m => m.cpi]];
    const rows = matching(), text = '\ufeff' + [columns.map(c => csvCell(c[0])).join(','),
      ...rows.map(m => columns.map(c => csvCell(c[1](m))).join(','))].join('\r\n');
    const url = URL.createObjectURL(new Blob([text], {type: 'text/csv;charset=utf-8;'})), a = node('a');
    a.href = url; a.download = 'ai-images-' + selected + '-' + period + '-' + R.report_date + '.csv';
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  selectProduct('all');
  // Health is small and independently mutable; never wait for it to show KPIs.
  loadStatus();
  if (typeof IntersectionObserver !== 'undefined') {
    const observer = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting)) { loadDetails(); observer.disconnect(); } }, {rootMargin: '100px'});
    observer.observe($('detail-status'));
  }
  const hashNeedsDetails = () => { if (['#groups', '#materials', '#method'].includes(location.hash)) loadDetails(); };
  if (typeof window !== 'undefined') window.addEventListener('hashchange', () => { syncLatestLink(); hashNeedsDetails(); });
  hashNeedsDetails();
})();
