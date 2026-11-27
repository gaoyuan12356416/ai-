(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { page: 1, sort: 'data_date', direction: 'desc', auth: {}, request: 0, items: [], appliedQuery: '' };
  const label = channel => channel === 'facebook' ? 'Facebook' : 'X';
  const number = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
  const usd = cents => cents == null ? '—' : (cents / 100).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  const time = value => value ? new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  }) : '待采集';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);

  function setDates(days) {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date());
    const part = name => parts.find(value => value.type === name).value;
    const today = Date.UTC(Number(part('year')), Number(part('month')) - 1, Number(part('day')));
    const date = ago => new Date(today - ago * 86400000).toISOString().slice(0, 10);
    $('filters').elements.start.value = date(days);
    $('filters').elements.end.value = date(1);
  }

  async function api(path) {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 401) throw Error('请使用飞书登录后查看报表。');
    if (response.status === 403) throw Error('暂无社媒素材效果报表权限，请联系管理员。');
    const data = await response.json();
    if (!response.ok) throw Error(data.message || '读取失败，请稍后刷新。');
    return data;
  }

  function params() {
    const query = new URLSearchParams(new FormData($('filters')));
    for (const [key, value] of Object.entries({
      page: state.page, page_size: 50, sort: state.sort, direction: state.direction,
    })) query.set(key, value);
    return query;
  }

  function metric(row, key) {
    if (row[key] != null) return key === 'revenue_cents' ? usd(row[key]) : number(row[key]);
    const known = row.known?.[key];
    const subtotal = known == null ? '—' : (key === 'revenue_cents' ? usd(known) : number(known));
    return `${subtotal}<small>当日有数据 ${number(row.coverage?.[key] || 0)}/${number(row.post_count)} 帖</small>`;
  }

  function materialRow(row, index) {
    const metrics = ['views', 'likes', 'comments', 'shares', 'bookmarks', 'revenue_cents'];
    return `<tr><td>${escape(row.data_date)}</td><td>${label(row.channel)}</td><td><a href="#detail" data-detail="${index}">${escape(row.material_id)}</a>
      <div class="name" title="${escape(row.material_name)}">${escape(row.material_name || '未命名素材')}</div>
      <small>${escape(row.product || '产品待匹配')} · ${escape(row.language)} · ${escape(row.drama_name || row.content_id)}</small></td>
      <td>${number(row.post_count)}</td><td>${number(row.account_count)}</td>
      ${metrics.map(key => `<td>${metric(row, key)}</td>`).join('')}
      <td>${row.metrics_at ? escape(time(row.metrics_at)) : '无当日快照'}${row.missing_posts ? `<small>数据不完整 ${number(row.missing_posts)} 帖</small>` : ''}</td></tr>`;
  }

  function renderSummary(data) {
    const notes = [];
    if (data.meta.metrics_upstream_error === 'x_http_402') {
      notes.push('X API 额度已耗尽，部分互动数据暂缺。已保留采集结果，额度恢复后将继续定时更新。');
    } else if (data.meta.metrics_upstream_error) {
      notes.push('X 指标接口暂不可用，已保留采集结果并安排重试。');
    }
    if (data.meta.unavailable_channels?.length) notes.push(data.meta.unavailable_channels.map(label).join('、') + ' 快照暂不可用。');
    const fb = data.meta.channels?.facebook;
    if (fb?.metrics_upstream_error) notes.push('Facebook 互动采集暂缓，已保留已采集指标。');
    if (fb?.revenue_incomplete_sites?.length) notes.push('Facebook 部分收入日期尚未齐全，相关收入待核实。');
    if (data.meta.stale) notes.push('效果快照更新延迟，当前展示上一次结果。');
    if (data.meta.revenue_stale) notes.push('收入来源更新延迟，仅展示已核验数据日期的收入。');
    if (data.meta.dates.includes_today) notes.push('查询包含今天，今天的数据尚未完整。');
    const unavailable = Object.entries(data.meta.dates.availability).filter(([, value]) => !value.available).map(([key]) => label(key));
    if (unavailable.length) notes.push(unavailable.join('、') + ' 尚无每日数据，等待采集更新。');
    if (Object.values(data.coverage).some(count => count < data.post_count)) {
      notes.push('部分日期缺少互动快照或收入数据。汇总卡片为所选数据日期的已知小计，素材行显示当天覆盖数；缺失不计为零，也不回退到累计值。');
    }
    $('notice').textContent = notes.join('\n');
    $('notice').classList.toggle('hidden', !notes.length);
    const cards = [
      ['渠道素材数（去重）', number(data.material_count)], ['有数据天数', number(data.data_days)],
      ['已知浏览量', number(data.summary.views)], ['已知点赞量', number(data.summary.likes)],
      ['已知收入 USD', usd(data.summary.revenue_cents)],
    ];
    $('summary').innerHTML = cards.map(([name, value]) =>
      `<div class="kpi-card"><span class="kpi-label">${name}</span><strong class="kpi-value">${value}</strong></div>`
    ).join('');
    $('freshness').textContent = `数据日期 ${data.meta.dates.start} 至 ${data.meta.dates.end}　|　` + Object.entries(data.meta.channels).map(([key, value]) =>
      `${label(key)} 更新于 ${time(value.generated_at)}，收入截至 ${value.revenue_through}（UTC）`).join('　|　');
  }

  async function load() {
    const form = $('filters');
    if (!form.reportValidity()) return;
    if (form.elements.start.value > form.elements.end.value) {
      $('notice').textContent = '数据开始日期不能晚于结束日期。';
      $('notice').classList.remove('hidden');
      return;
    }
    const request = ++state.request;
    const query = params();
    $('rows').innerHTML = '<tr><td class="empty-table" colspan="12">正在读取每日素材效果…</td></tr>';
    $('prev').disabled = $('next').disabled = true;
    try {
      const data = await api('/api/x-material-effects?' + query);
      if (request !== state.request) return;
      state.items = data.items;
      state.appliedQuery = query.toString();
      $('rows').innerHTML = data.items.map(materialRow).join('') ||
        '<tr><td class="empty-table" colspan="12">所选数据日期内没有符合条件的每日数据。历史互动无法从累计值还原，尚未采集或超出保留范围的日期不会按零展示。</td></tr>';
      $('count').textContent = `共 ${number(data.total)} 条日期素材记录 · 第 ${number(data.page)} 页`;
      $('prev').disabled = data.page <= 1;
      $('next').disabled = data.page * data.page_size >= data.total;
      renderSummary(data);
    } catch (error) {
      if (request !== state.request) return;
      state.items = [];
      $('rows').innerHTML = `<tr><td class="empty-table error" colspan="12">${escape(error.message)}</td></tr>`;
      for (const id of ['summary', 'freshness', 'count']) $(id).textContent = '';
      $('notice').classList.add('hidden');
    }
  }

  const sortButtons = document.querySelectorAll('[data-sort]');
  for (const button of sortButtons) button.addEventListener('click', () => {
    const key = button.dataset.sort;
    state.direction = state.sort === key && state.direction === 'desc' ? 'asc' : 'desc';
    state.sort = key;
    for (const candidate of sortButtons) {
      candidate.classList.toggle('sorted', candidate === button);
      candidate.closest('th').removeAttribute('aria-sort');
    }
    button.closest('th').setAttribute('aria-sort', state.direction === 'desc' ? 'descending' : 'ascending');
    state.page = 1;
    void load();
  });

  function postRow(post) {
    const url = post.channel === 'facebook'
      ? (/^https:\/\/www\.facebook\.com\//.test(post.post_url) ? post.post_url : 'https://www.facebook.com/')
      : 'https://x.com/i/status/' + encodeURIComponent(post.post_id);
    return `<tr><td>${escape(post.data_date)}</td><td>${label(post.channel)}</td><td><a href="${escape(url)}" target="_blank" rel="noopener">${escape(post.post_id)}</a>
      <small>${escape(post.account_username || post.account_id)} · ${escape(post.source)}</small></td>
      ${['views', 'likes', 'comments', 'shares', 'reposts', 'quotes', 'bookmarks'].map(key => `<td>${number(post[key])}</td>`).join('')}
      <td>${usd(post.revenue_cents)}</td><td>${post.metrics_at ? escape(time(post.metrics_at)) : '无当日快照'}</td></tr>`;
  }

  let detailRequest = 0;
  async function loadDetail(row, page) {
    const request = ++detailRequest;
    const query = new URLSearchParams(state.appliedQuery);
    for (const [key, value] of Object.entries({
      view: 'posts', channel: row.channel, namespace: row.namespace, material_id: row.material_id,
      start: row.data_date, end: row.data_date, page, sort: 'views', direction: 'desc',
    })) query.set(key, value);
    $('detailTitle').textContent = `${row.data_date} · ${label(row.channel)} · 素材 ${row.material_id}`;
    if (!$('detail').open) $('detail').showModal();
    $('detailBody').textContent = '正在读取…';
    try {
      const data = await api('/api/x-material-effects?' + query);
      if (request !== detailRequest || !$('detail').open) return;
      const headings = ['数据日期', '渠道', '帖子／账号', '新增浏览', '赞', '评', '分享', 'X 转发', 'X 引用', '收藏', '收入 USD', '当日互动采样'];
      $('detailBody').innerHTML = `<p>当天可见关联帖子 ${number(data.total)} 条，当前显示 ${number(data.items.length)} 条。按账号或发布归属展示；指标为当天新增，缺失显示“—”。</p>
        <div class="analytics-table-scroll" tabindex="0"><table class="analytics-table">
        <thead><tr>${headings.map(name => `<th scope="col" class="plain-heading">${name}</th>`).join('')}</tr></thead>
        <tbody>${data.items.map(postRow).join('')}</tbody></table></div>
        <div class="report-pagination"><span>第 ${number(data.page)} 页</span><div>
        <button type="button" class="btn btn-secondary btn-sm" id="detailPrev">上一页</button>
        <button type="button" class="btn btn-secondary btn-sm" id="detailNext">下一页</button></div></div>`;
      $('detailPrev').disabled = data.page <= 1;
      $('detailNext').disabled = data.page * data.page_size >= data.total;
      $('detailPrev').onclick = () => loadDetail(row, page - 1);
      $('detailNext').onclick = () => loadDetail(row, page + 1);
    } catch (error) {
      if (request === detailRequest) $('detailBody').textContent = error.message;
    }
  }

  $('filters').addEventListener('submit', event => { event.preventDefault(); state.page = 1; void load(); });
  $('filters').addEventListener('reset', () => setTimeout(() => { setDates(7); state.page = 1; void load(); }, 0));
  for (const button of document.querySelectorAll('[data-days]')) button.addEventListener('click', () => {
    setDates(Number(button.dataset.days)); state.page = 1; void load();
  });
  $('prev').onclick = () => { state.page--; void load(); };
  $('next').onclick = () => { state.page++; void load(); };
  $('rows').addEventListener('click', event => {
    const link = event.target.closest('[data-detail]');
    if (!link) return;
    event.preventDefault();
    void loadDetail(state.items[Number(link.dataset.detail)], 1);
  });
  $('closeDetail').onclick = () => $('detail').close();
  $('authButton').onclick = () => UiTopbar.handleAuthAction({ auth: state.auth });

  async function init() {
    setDates(7);
    const controls = { userCard: '#userCard', authButton: '#authButton', refreshButton: '#refreshPage' };
    UiTopbar.render({ ...controls, auth: state.auth });
    try {
      state.auth = await api('/api/ui/topbar');
      UiTopbar.render({ ...controls, auth: state.auth });
      void QuickNav.render({ container: '#quickNav', auth: state.auth, activeKey: 'xMaterialEffects' })
        .catch(() => { $('quickNav').textContent = '导航加载失败，请刷新重试。'; });
      await load();
    } catch (error) {
      $('notice').textContent = error.message;
      $('notice').classList.remove('hidden');
    }
  }
  void init();
})();
