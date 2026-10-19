(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const API = '/api/youtube-analytics';
  const dimensions = {owner:'生成人',drama:'剧目',channel:'频道',link_type:'链接类型',language:'语言',date:'日期'};
  const filterKeys = ['owner','drama','channel','link_type','language'];
  const metrics = {
    clicks:{label:'落地页点击',hint:'链接带来的点击次数',color:'#4d7ddd'},
    views:{label:'落地页访问',hint:'落地页访问次数',color:'#829acc'},
    installs:{label:'安装数',hint:'归因安装量',color:'#54a3a0'},
    conversions:{label:'付费转化数',hint:'每日充值人数累加 · 跨日不去重',color:'#aa93cf'},
    revenue:{label:'收入 (USD)',hint:'收入金额 · 未扣除退款',color:'#ceac67',money:true},
    refunds:{label:'退款 (USD)',hint:'退款金额',color:'#c39496',money:true},
    install_rate:{label:'安装率',percent:true},pay_rate:{label:'付费率',percent:true},links:{label:'生成链接数'}
  };
  const kpiKeys = ['clicks','views','installs','conversions','revenue','refunds'];
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const format = (value,key) => !finite(value) ? '—' : metrics[key]?.money ? '$' + Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : metrics[key]?.percent ? Number(value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) + '%' : Number(value).toLocaleString('en-US',{maximumFractionDigits:2});
  const utcDate = date => date.toISOString().slice(0,10);
  const stamp = value => { const date = new Date(value); return value && Number.isFinite(date.getTime()) ? date.toISOString().replace('T',' ').slice(0,19) + ' UTC' : '未提供'; };
  let auth, options = {}, report = null, applied = null, allowed = false, dirty = false, busy = false;
  let epoch = 0, serial = 0, exportSerial = 0, exporting = false;
  let reportController, exportController;

  function message(text,kind = '') {
    const node = $('#page-message'); node.textContent = text; node.className = 'analytics-message' + (kind ? ' ' + kind : ''); node.hidden = !text;
  }
  function scopeLabel(scope) { $('#scope-label').textContent = scope === 'tenant' ? '数据范围：本租户全部生成人（管理员）' : '数据范围：仅本人生成的链接'; }
  function clearReport() {
    report = null;
    $('#report-content').classList.add('hidden');
    for (const id of ['kpi-grid','trend-chart','conversion-chart','ranking-chart','report-head','report-body','report-period','report-updated','page-summary','page-number','quality-message','ranking-caption','table-description','trend-caption']) $('#' + id).replaceChildren();
    $('#quality-message').hidden = true;
    updateControls();
  }
  function denied(error) {
    ++epoch; ++serial; ++exportSerial;
    reportController?.abort(); exportController?.abort();
    allowed = false; busy = false; exporting = false; applied = null; options = {};
    clearReport(); $('#dimension-filters').replaceChildren(); $('#filter-fields').disabled = true;
    $('#scope-label').textContent = '数据权限不可用';
    message(error.message || (error.status === 401 ? '登录已失效，请重新使用飞书登录。' : '暂无 YouTube 数据报表权限，请联系管理员。'),'error');
    if (error.status === 401) { auth = {...auth,authenticated:false}; UiTopbar.render({auth,userCard:'#userCard',authButton:'#authButton'}); }
  }
  async function request(path,{signal,raw = false,...init} = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort(); else signal?.addEventListener('abort',abort,{once:true});
    const timeout = setTimeout(abort,60000);
    try {
      const response = await fetch(path,{credentials:'same-origin',cache:'no-store',...init,signal:controller.signal,headers:{Accept:raw ? 'text/csv' : 'application/json',...(init.body ? {'Content-Type':'application/json'} : {})}});
      if (!response.ok) {
        let payload; try { payload = await response.json(); } catch (_) { payload = {}; }
        const error = new Error(payload.message || (response.status === 401 ? '登录已失效，请重新使用飞书登录。' : response.status === 403 ? '暂无 YouTube 数据报表权限，请联系管理员。' : '数据读取失败，请稍后重试（' + response.status + '）。'));
        error.status = response.status; throw error;
      }
      if (raw) {
        if (!/\btext\/csv\b/i.test(response.headers.get('Content-Type') || '')) throw new Error('导出响应格式异常，请刷新后重试。');
        return await response.blob();
      }
      try { return await response.json(); } catch (_) { throw new Error('服务返回了无效数据，请稍后重试。'); }
    } catch (error) {
      if (error.name === 'AbortError' && !signal?.aborted) throw new Error('请求超时，请稍后重试。');
      throw error;
    } finally { clearTimeout(timeout); signal?.removeEventListener('abort',abort); }
  }
  function updateControls() {
    const usable = allowed && !!report && !busy && !dirty;
    $('#export-report').disabled = !usable || exporting;
    $('#export-report').innerHTML = exporting ? '正在导出…' : '<span aria-hidden="true">↓</span> 导出全部分组 CSV';
    $('#previous-page').disabled = !usable || Number(report?.pagination?.page) <= 1;
    $('#next-page').disabled = !usable || Number(report?.pagination?.page) >= Number(report?.pagination?.pages);
    $('#page-size').disabled = !usable; $('#ranking-metric').disabled = !usable;
    $('#trend-metric').disabled = !report || busy;
    $('#apply-filters').textContent = busy ? '正在查询…' : '应用筛选';
    $('#report-content').classList.toggle('is-dirty',dirty);
    $('#report-content').setAttribute('aria-busy',String(busy));
    $('#report-head').querySelectorAll('button').forEach(button => { button.disabled = !usable; });
  }
  function markDirty() {
    if (!allowed) return;
    dirty = true;
    // Prevent a pending query/export from becoming the result of newly edited filters.
    if (busy) { ++serial; reportController?.abort(); busy = false; clearReport(); message('筛选条件已修改，请点击「应用筛选」查询。'); }
    if (exporting) { ++exportSerial; exportController?.abort(); exporting = false; }
    $('#filter-hint').textContent = '筛选尚未应用；请点击「应用筛选」更新数据。'; $('#filter-hint').className = 'dirty-hint'; updateControls();
  }
  function dateRange(days) {
    const end = new Date(); end.setUTCHours(0,0,0,0); end.setUTCDate(end.getUTCDate()-1);
    const start = new Date(end); start.setUTCDate(start.getUTCDate()-days+1);
    $('#date-start').value = utcDate(start); $('#date-end').value = utcDate(end);
  }
  function renderFilterOptions() {
    $('#dimension-filters').innerHTML = filterKeys.map(key => `<details class="multi-filter" data-dimension="${key}"><summary><span>${dimensions[key]}</span><strong data-count>全部</strong></summary><div class="filter-popover"><input type="search" class="option-search" aria-label="搜索${dimensions[key]}选项" placeholder="搜索${dimensions[key]}"><div class="filter-option-list">${(options[key] || []).map(item => `<label class="filter-option"><input type="checkbox" name="${key}" value="${esc(item.value)}"><span>${esc(item.label || item.value)}</span></label>`).join('')}<div class="filter-empty" ${(options[key] || []).length ? 'hidden' : ''}>暂无可选项</div></div><button type="button" class="filter-clear">清除选择</button></div></details>`).join('');
  }
  function renderGroupOptions() {
    $('#group-choices').innerHTML = Object.entries(dimensions).map(([key,label]) => `<label class="group-choice"><input type="checkbox" name="group" value="${key}" ${['owner','drama','channel'].includes(key) ? 'checked' : ''}><span>${label}</span></label>`).join('');
  }
  function selectedGroups() { return Array.from(document.querySelectorAll('[name="group"]:checked'),n => n.value); }
  function updateGroupLimit() { const count = selectedGroups().length; document.querySelectorAll('[name="group"]').forEach(input => { input.disabled = !input.checked && count >= 3; }); }
  function readQuery() {
    const start = $('#date-start').value, end = $('#date-end').value;
    if (!start || !end || start > end) throw new Error('请选择有效的日期范围，开始日期不能晚于结束日期。');
    const groups = selectedGroups(); if (!groups.length || groups.length > 3) throw new Error('请选择 1 至 3 个分组维度。');
    const query = new URLSearchParams({start,end,group_by:groups.join(','),sort:'clicks',direction:'desc',page:'1',page_size:$('#page-size').value,ranking_metric:$('#ranking-metric').value || 'clicks'});
    for (const key of filterKeys) document.querySelectorAll(`input[name="${key}"]:checked`).forEach(input => query.append(key,input.value));
    const search = $('#report-search').value.trim(); if (search) query.set('search',search);
    return query;
  }
  function validateReport(value) {
    if (!value || !value.totals || !Array.isArray(value.rows) || !Array.isArray(value.trend) || !Array.isArray(value.ranking) || !Array.isArray(value.group_by) || !value.group_by.length || value.group_by.some(key => !Object.hasOwn(dimensions,key)) || !value.pagination || !value.meta || !value.quality) throw new Error('报表响应不完整，请刷新后重试。');
    if (![value.pagination.page,value.pagination.pages,value.pagination.total,value.pagination.page_size].every(finite)) throw new Error('分页数据无效，请刷新后重试。');
    return value;
  }
  async function loadReport(query) {
    if (!allowed) return;
    const current = ++serial, generation = epoch;
    reportController?.abort(); exportController?.abort(); ++exportSerial; exporting = false;
    reportController = new AbortController(); busy = true; dirty = false; applied = new URLSearchParams(query);
    $('#filter-hint').textContent = '按 UTC 自然日统计；筛选后点击「应用筛选」生效。'; $('#filter-hint').className = '';
    document.querySelectorAll('.multi-filter[open]').forEach(node => { node.open = false; });
    clearReport(); message('正在查询报表，请稍候…');
    try {
      const result = await request(API + '/report?' + query,{signal:reportController.signal});
      if (current !== serial || generation !== epoch) return;
      report = validateReport(result); busy = false; renderReport(); message('');
    } catch (error) {
      if (current !== serial || generation !== epoch) return;
      busy = false;
      if (error.status === 401 || error.status === 403) denied(error);
      else { clearReport(); message(error.message || '数据读取失败，请重试。','error'); }
    } finally { if (current === serial && generation === epoch) { busy = false; updateControls(); } }
  }
  function renderQuality() {
    const quality = report.quality, warnings = [];
    const missing = Array.isArray(quality.missing_dates) ? quality.missing_dates : [];
    if (missing.length) warnings.push(`有 ${missing.length} 个日期未同步：${missing.slice(0,10).join('、')}${missing.length > 10 ? ' 等' : ''}。汇总仅包含已有日期的数据，未同步日期不会按 0 处理。`);
    if (Number(quality.unmatched_campaigns) > 0) warnings.push(`有 ${format(quality.unmatched_campaigns)} 条归因记录无法唯一归属，未计入当前汇总。`);
    if (Number(quality.excluded_campaigns) > 0) warnings.push(`另有 ${format(quality.excluded_campaigns)} 条归因记录因归属校验被排除。`);
    for (const warning of report.warnings || []) warnings.push(typeof warning === 'string' ? warning : warning.message || warning.code || '部分源数据暂不可用。');
    $('#quality-message').textContent = warnings.join(' '); $('#quality-message').hidden = !warnings.length;
  }
  function renderKpis() {
    $('#kpi-grid').innerHTML = kpiKeys.map(key => `<article class="kpi-card" style="--metric-color:${metrics[key].color}"><span class="kpi-label"><i aria-hidden="true"></i>${metrics[key].label}</span><strong class="kpi-value" data-metric="${key}">${format(report.totals[key],key)}</strong><span class="kpi-hint">${metrics[key].hint}</span></article>`).join('');
  }
  function emptyChart(title,detail = '') { return `<div class="chart-empty"><strong>${esc(title)}</strong>${detail ? `<span>${esc(detail)}</span>` : ''}</div>`; }
  function renderTrend() {
    if (!report) return;
    const key = $('#trend-metric').value, points = report.trend;
    const available = points.filter(row => finite(row[key]));
    $('#trend-caption').textContent = metrics[key].label + (metrics[key].money ? ' · USD' : '') + ' · 将鼠标悬停或键盘聚焦数据点查看详情；断点表示该日期未同步。';
    if (!available.length) { $('#trend-chart').innerHTML = emptyChart('暂无可展示的趋势',points.length ? '所选日期的数据尚未同步' : '当前筛选没有可用的日期数据'); return; }
    const width = 760,height = 260,left = 67,right = 19,top = 20,bottom = 39;
    const values = available.map(row => Number(row[key]));
    const minimum = Math.min(0,...values),maximum = Math.max(1,...values),range = maximum-minimum;
    const x = index => left + (points.length === 1 ? (width-left-right)/2 : index*(width-left-right)/(points.length-1));
    const y = value => top + (maximum-value)/range*(height-top-bottom);
    const compact = value => Math.abs(value) >= 1000000 ? (value/1000000).toFixed(1) + 'm' : Math.abs(value) >= 1000 ? (value/1000).toFixed(1) + 'k' : Number(value.toFixed(2)).toString();
    let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(metrics[key].label)}按日趋势"><title>${esc(metrics[key].label)}按 UTC 日期的趋势</title><defs><linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#5d8bdd" stop-opacity=".18"/><stop offset="100%" stop-color="#5d8bdd" stop-opacity=".01"/></linearGradient></defs>`;
    for (let step = 0; step < 5; step++) { const value = minimum + range*step/4, position = y(value); svg += `<line x1="${left}" y1="${position}" x2="${width-right}" y2="${position}" stroke="#eaf0f8" stroke-dasharray="3 5"/><text x="${left-12}" y="${position+4}" text-anchor="end">${metrics[key].money ? '$' : ''}${compact(value)}</text>`; }
    const segments = []; let segment = [];
    points.forEach((row,index) => { if (finite(row[key])) segment.push([x(index),y(Number(row[key]))]); else if (segment.length) { segments.push(segment); segment = []; } }); if (segment.length) segments.push(segment);
    for (const part of segments) { const path = part.map(([px,py],index) => `${index ? 'L' : 'M'}${px},${py}`).join(' '); if (part.length > 1) svg += `<path d="${path} L${part.at(-1)[0]},${y(0)} L${part[0][0]},${y(0)} Z" fill="url(#trend-fill)"/><path d="${path}" stroke="#5080d7" stroke-width="2.5"/>`; }
    const labelEvery = Math.max(1,Math.ceil(points.length/6));
    points.forEach((row,index) => {
      if (index % labelEvery === 0 || index === points.length-1) svg += `<text x="${x(index)}" y="${height-12}" text-anchor="middle">${esc(String(row.date || '').slice(5))}</text>`;
      if (finite(row[key])) svg += `<circle class="trend-dot" cx="${x(index)}" cy="${y(Number(row[key]))}" r="3.5" tabindex="0" aria-label="${esc(row.date)} ${metrics[key].label} ${format(row[key],key)}"><title>${esc(row.date)} · ${metrics[key].label}：${format(row[key],key)}</title></circle>`;
      else svg += `<text x="${x(index)}" y="${height-bottom-6}" text-anchor="middle"><title>${esc(row.date)} · 未同步</title>—</text>`;
    });
    $('#trend-chart').innerHTML = svg + '</svg>';
  }
  function renderConversion() {
    const stages = [{key:'clicks',label:'落地页点击',color:'#5c89dc'},{key:'installs',label:'安装',color:'#76aaa9',rate:'install_rate',rateLabel:'点击 → 安装'},{key:'conversions',label:'付费转化',color:'#a896ca',rate:'pay_rate',rateLabel:'安装 → 付费'}];
    const max = Math.max(1,...stages.map(stage => finite(report.totals[stage.key]) ? Number(report.totals[stage.key]) : 0));
    $('#conversion-chart').innerHTML = stages.map(stage => { const value = report.totals[stage.key],size = finite(value) ? Math.max(0,Number(value))/max*100 : 0; return `<div class="conversion-stage"><div class="conversion-stage-head"><span>${stage.label}</span><strong>${format(value,stage.key)}</strong></div><div class="conversion-track" aria-hidden="true"><span style="width:${size}%;background:${stage.color}"></span></div><div class="conversion-rate"><span>${stage.rateLabel || '所选日期合计'}</span><span>${stage.rate ? format(report.totals[stage.rate],stage.rate) : finite(value) ? '' : '数据未同步'}</span></div></div>`; }).join('');
  }
  function renderRanking() {
    const key = applied.get('ranking_metric'),rows = report.ranking.slice(0,10);
    $('#ranking-caption').textContent = report.group_by.map(group => dimensions[group]).join(' × ') + ' · 按' + metrics[key].label + '排序';
    if (!rows.length || rows.every(row => !finite(row[key]))) { $('#ranking-chart').innerHTML = emptyChart('暂无可展示的分组排名','请调整日期或筛选条件'); return; }
    const max = Math.max(1,...rows.map(row => finite(row[key]) ? Math.abs(Number(row[key])) : 0));
    $('#ranking-chart').innerHTML = rows.map((row,index) => `<div class="ranking-row"><span class="ranking-position">${index+1}</span><div class="ranking-content"><div class="ranking-copy"><span title="${esc(row.label)}">${esc(row.label || '未标记分组')}</span><strong>${format(row[key],key)}</strong></div><div class="ranking-track" aria-hidden="true"><span style="width:${finite(row[key]) ? Math.abs(Number(row[key]))/max*100 : 0}%"></span></div></div></div>`).join('');
  }
  function renderTable() {
    const groups = report.group_by,columns = [...groups,...Object.keys(metrics)];
    const sort = applied.get('sort'),direction = applied.get('direction');
    $('#table-description').textContent = '分组：' + groups.map(group => dimensions[group]).join(' × ') + ' · 点击列标题排序 · 期内生成链接 ' + format(report.totals.links) + ' 条';
    $('#report-head').innerHTML = '<tr>' + columns.map(key => `<th scope="col" class="${metrics[key] ? 'number-column' : ''}" aria-sort="${key === sort ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}"><button type="button" data-sort="${key}" class="${key === sort ? 'sorted' : ''}">${dimensions[key] || metrics[key].label}<span aria-hidden="true"> ${key === sort ? direction === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>`).join('') + '</tr>';
    $('#report-body').innerHTML = report.rows.length ? report.rows.map(row => '<tr>' + groups.map(key => {
      const value = row[key],label = row[key + '_label'] || value || '未标记';
      return `<td class="dimension-cell"><strong>${esc(label)}</strong>${['drama','channel'].includes(key) ? `<small>${key === 'drama' ? '剧 ID：' : '频道 ID：'}${esc(value || '—')}</small>` : ''}</td>`;
    }).join('') + Object.keys(metrics).map(key => `<td class="number-column">${format(row[key],key)}</td>`).join('') + '</tr>').join('') : `<tr><td colspan="${columns.length}" class="empty-table">当前条件下没有可归属的数据，请调整日期或筛选条件。</td></tr>`;
    const {page,pages,total} = report.pagination;
    $('#page-summary').textContent = '共 ' + format(total) + ' 组' + (report.rows.length ? ' · 本页 ' + report.rows.length + ' 组' : '');
    $('#page-number').textContent = pages > 0 ? page + ' / ' + pages : '0 / 0';
  }
  function renderReport() {
    $('#report-content').classList.remove('hidden'); scopeLabel(report.meta.scope);
    $('#report-period').textContent = (report.meta.start || applied.get('start')) + ' — ' + (report.meta.end || applied.get('end')) + ' · UTC';
    $('#report-updated').textContent = '源数据更新：' + stamp(report.meta.source_updated_at) + ' · 查询时间：' + stamp(report.meta.fetched_at);
    renderQuality(); renderKpis(); renderTrend(); renderConversion(); renderRanking(); renderTable(); updateControls();
  }
  async function exportCsv() {
    if (!report || !allowed || busy || dirty || exporting) return;
    const current = ++exportSerial,generation = epoch,reportSerial = serial,query = new URLSearchParams(applied);
    exporting = true; exportController = new AbortController(); updateControls();
    try {
      const blob = await request(API + '/export.csv?' + query,{raw:true,signal:exportController.signal});
      if (current !== exportSerial || generation !== epoch || reportSerial !== serial || dirty) return;
      const url = URL.createObjectURL(blob),anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'youtube-analytics_' + query.get('start') + '_' + query.get('end') + '.csv';
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url),1000);
      message('已导出当前筛选下的全部分组数据。');
    } catch (error) {
      if (current !== exportSerial || generation !== epoch) return;
      if (error.status === 401 || error.status === 403) denied(error); else if (error.name !== 'AbortError') message(error.message || '导出失败，请重试。','error');
    } finally { if (current === exportSerial) { exporting = false; updateControls(); } }
  }
  async function initialize() {
    const generation = ++epoch; ++serial; ++exportSerial;
    reportController?.abort(); exportController?.abort(); allowed = false; busy = false; exporting = false; dirty = false;
    clearReport(); $('#filter-fields').disabled = true; message('正在核验权限并加载筛选项…');
    try {
      auth = await request('/api/ui/topbar'); if (generation !== epoch) return;
      UiTopbar.render({auth,userCard:'#userCard',authButton:'#authButton'});
      await Promise.resolve(QuickNav.render({container:'#quickNav',auth,activeKey:'youtubeAnalytics'})).catch(() => {});
      if (generation !== epoch) return;
      if (!auth.authenticated) { denied({status:401,message:'请先使用飞书登录后台，再查看 YouTube 数据报表。'}); return; }
      const result = await request(API + '/options'); if (generation !== epoch) return;
      if (!result.options || filterKeys.some(key => !Array.isArray(result.options[key]))) throw new Error('筛选数据不完整，请点击「刷新数据」重试。');
      options = result.options; allowed = true; renderFilterOptions(); renderGroupOptions(); updateGroupLimit(); scopeLabel(result.scope);
      $('#filter-fields').disabled = false; dateRange(7); $('#report-search').value = ''; $('#page-size').value = '50';
      $('#ranking-metric').value = 'clicks';
      await loadReport(readQuery());
    } catch (error) { if (generation !== epoch) return; if (error.status === 401 || error.status === 403) denied(error); else message(error.message || '报表初始化失败，请点击「刷新数据」重试。','error'); }
  }
  const metricOptions = kpiKeys.map(key => `<option value="${key}">${metrics[key].label}</option>`).join('');
  $('#trend-metric').innerHTML = metricOptions; $('#ranking-metric').innerHTML = metricOptions;
  $('#report-filters').addEventListener('submit',event => { event.preventDefault(); try { void loadReport(readQuery()); } catch (error) { message(error.message,'error'); } });
  $('#report-filters').addEventListener('change',event => {
    if (event.target.name === 'group') updateGroupLimit();
    const detail = event.target.closest('.multi-filter'); if (detail) { const count = detail.querySelectorAll('input[type=checkbox]:checked').length; detail.querySelector('[data-count]').textContent = count ? '已选 ' + count : '全部'; }
    markDirty();
  });
  $('#report-search').addEventListener('input',markDirty);
  $('#dimension-filters').addEventListener('input',event => {
    if (!event.target.classList.contains('option-search')) return;
    const query = event.target.value.trim().toLowerCase(),detail = event.target.closest('.multi-filter'); let visible = 0;
    detail.querySelectorAll('.filter-option').forEach(node => { const match = (node.textContent + ' ' + node.querySelector('input').value).toLowerCase().includes(query); node.hidden = !match; if (match) visible++; });
    const empty = detail.querySelector('.filter-empty'); empty.hidden = !!visible; empty.textContent = query ? '没有匹配选项' : '暂无可选项';
  });
  $('#dimension-filters').addEventListener('click',event => { const button = event.target.closest('.filter-clear'); if (!button) return; const detail = button.closest('.multi-filter'); detail.querySelectorAll('input[type=checkbox]').forEach(input => { input.checked = false; }); detail.querySelector('[data-count]').textContent = '全部'; markDirty(); });
  document.querySelectorAll('[data-days]').forEach(button => button.addEventListener('click',() => { dateRange(Number(button.dataset.days)); markDirty(); }));
  $('#reset-filters').addEventListener('click',() => { dateRange(7); $('#report-search').value = ''; renderFilterOptions(); renderGroupOptions(); updateGroupLimit(); markDirty(); });
  $('#trend-metric').addEventListener('change',renderTrend);
  $('#ranking-metric').addEventListener('change',() => { if (!applied || dirty) return; const query = new URLSearchParams(applied); query.set('ranking_metric',$('#ranking-metric').value); void loadReport(query); });
  $('#page-size').addEventListener('change',() => { if (!applied || dirty) return; const query = new URLSearchParams(applied); query.set('page_size',$('#page-size').value); query.set('page','1'); void loadReport(query); });
  $('#report-head').addEventListener('click',event => { const button = event.target.closest('[data-sort]'); if (!button || !report || busy || dirty) return; const query = new URLSearchParams(applied),key = button.dataset.sort; query.set('direction',query.get('sort') === key && query.get('direction') === 'desc' ? 'asc' : 'desc'); query.set('sort',key); query.set('page','1'); void loadReport(query); });
  for (const [id,delta] of [['previous-page',-1],['next-page',1]]) $('#' + id).addEventListener('click',() => { if (!report || busy || dirty) return; const query = new URLSearchParams(applied); query.set('page',String(Number(report.pagination.page)+delta)); void loadReport(query); });
  $('#export-report').addEventListener('click',exportCsv);
  $('#refreshPage').addEventListener('click',() => { if (allowed && applied && !dirty) void loadReport(new URLSearchParams(applied)); else void initialize(); });
  $('#authButton').addEventListener('click',() => { const currentAuth = auth; if (currentAuth?.authenticated) denied({status:401,message:'正在退出登录…'}); void UiTopbar.handleAuthAction({auth:currentAuth,api:request}); });
  document.addEventListener('click',event => { document.querySelectorAll('.multi-filter[open]').forEach(node => { if (!node.contains(event.target)) node.open = false; }); });
  document.addEventListener('keydown',event => { if (event.key === 'Escape') document.querySelectorAll('.multi-filter[open]').forEach(node => { node.open = false; }); });
  window.addEventListener('pagehide',() => { ++epoch; ++serial; ++exportSerial; reportController?.abort(); exportController?.abort(); });
  void initialize();
})();
