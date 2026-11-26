(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { page: 1, sort: 'views', direction: 'desc', auth: {}, request: 0, items: [] };
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
    return `${subtotal}<small>已采集 ${number(row.coverage?.[key] || 0)}/${number(row.post_count)}</small>`;
  }

  function materialRow(row, index) {
    const metrics = ['views', 'likes', 'comments', 'shares', 'bookmarks', 'revenue_cents'];
    return `<tr><td>${label(row.channel)}</td><td><a href="#detail" data-detail="${index}">${escape(row.material_id)}</a>
      <div class="name" title="${escape(row.material_name)}">${escape(row.material_name || '未命名素材')}</div>
      <small>${escape(row.product || '产品待匹配')} · ${escape(row.language)} · ${escape(row.drama_name || row.content_id)}</small></td>
      <td>${number(row.post_count)}</td><td>${number(row.account_count)}</td>
      ${metrics.map(key => `<td>${metric(row, key)}</td>`).join('')}
      <td>${escape(time(row.metrics_at))}${row.stale_posts ? `<small>采集异常 ${number(row.stale_posts)} 帖</small>` : ''}</td></tr>`;
  }

  function renderSummary(data) {
    const notes = [];
    if (data.meta.metrics_upstream_error === 'x_http_402') {
      notes.push('X API 额度已耗尽，部分互动数据暂缺。已保留采集结果，额度恢复后将继续定时更新。');
    } else if (data.meta.metrics_upstream_error) {
      notes.push('X 指标接口暂不可用，已保留采集结果并安排重试。');
    }
    const fb = data.meta.channels?.facebook;
    if (fb?.metrics_upstream_error) notes.push('Facebook 互动采集暂缓，已保留已采集指标。');
    if (fb?.revenue_incomplete_sites?.length) notes.push('Facebook 部分收入日期尚未齐全，相关收入待核实。');
    if (data.meta.stale) notes.push('效果快照更新延迟，当前展示上一次结果。');
    if (data.meta.revenue_stale) notes.push('收入来源更新延迟，收入值保留上次完整快照。');
    if (Object.values(data.coverage).some(count => count < data.post_count)) {
      notes.push('部分帖子指标尚未采集或无法获取。汇总卡片为已知小计，素材行显示覆盖数，缺失不计为零。');
    }
    $('notice').textContent = notes.join('\n');
    $('notice').classList.toggle('hidden', !notes.length);
    const cards = [
      ['渠道素材数', number(data.total)], ['帖子数', number(data.post_count)],
      ['已知浏览量', number(data.summary.views)], ['已知点赞量', number(data.summary.likes)],
      ['已知收入 USD', usd(data.summary.revenue_cents)],
    ];
    $('summary').innerHTML = cards.map(([name, value]) =>
      `<div class="kpi-card"><span class="kpi-label">${name}</span><strong class="kpi-value">${value}</strong></div>`
    ).join('');
    $('freshness').textContent = Object.entries(data.meta.channels).map(([key, value]) =>
      `${label(key)} 更新于 ${time(value.generated_at)}，收入截至 ${value.revenue_through}（UTC）`).join('　|　');
  }

  async function load() {
    const request = ++state.request;
    $('rows').innerHTML = '<tr><td class="empty-table" colspan="11">正在读取素材效果…</td></tr>';
    $('prev').disabled = $('next').disabled = true;
    try {
      const data = await api('/api/x-material-effects?' + params());
      if (request !== state.request) return;
      state.items = data.items;
      $('rows').innerHTML = data.items.map(materialRow).join('') ||
        '<tr><td class="empty-table" colspan="11">没有符合条件的已发布素材。</td></tr>';
      $('count').textContent = `共 ${number(data.total)} 个渠道素材 · 第 ${number(data.page)} 页`;
      $('prev').disabled = data.page <= 1;
      $('next').disabled = data.page * data.page_size >= data.total;
      renderSummary(data);
    } catch (error) {
      if (request !== state.request) return;
      state.items = [];
      $('rows').innerHTML = `<tr><td class="empty-table error" colspan="11">${escape(error.message)}</td></tr>`;
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
    return `<tr><td>${label(post.channel)}</td><td><a href="${escape(url)}" target="_blank" rel="noopener">${escape(post.post_id)}</a>
      <small>${escape(post.account_username || post.account_id)} · ${escape(post.source)}</small></td><td>${time(post.published_at)}</td>
      ${['views', 'likes', 'comments', 'shares', 'reposts', 'quotes'].map(key => `<td>${number(post[key])}</td>`).join('')}
      <td>${usd(post.revenue_cents)}</td><td>${escape(post.error_code || (post.metrics_at ? '已采集' : '待采集'))}<small>${time(post.metrics_at)}</small></td></tr>`;
  }

  let detailRequest = 0;
  async function loadDetail(row, page) {
    const request = ++detailRequest;
    const query = params();
    for (const [key, value] of Object.entries({
      view: 'posts', channel: row.channel, namespace: row.namespace, material_id: row.material_id, page, sort: 'views',
    })) query.set(key, value);
    if (!$('detail').open) $('detail').showModal();
    $('detailBody').textContent = '正在读取…';
    try {
      const data = await api('/api/x-material-effects?' + query);
      if (request !== detailRequest || !$('detail').open) return;
      const headings = ['渠道', '帖子／账号', '发布时间', '浏览', '赞', '评', '分享', 'X 转发', 'X 引用', '收入 USD', '采集状态'];
      $('detailBody').innerHTML = `<p>可见帖子 ${number(data.total)} 条，当前显示 ${number(data.items.length)} 条。明细按账号或发布归属展示。</p>
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
  $('filters').addEventListener('reset', () => setTimeout(() => { state.page = 1; void load(); }, 0));
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
