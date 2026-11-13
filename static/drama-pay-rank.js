(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const names={en:'英语',es:'西班牙语',pt:'葡萄牙语',ar:'阿拉伯语',bn:'孟加拉语',cs:'捷克语',de:'德语',el:'希腊语',fr:'法语',hi:'印地语',hu:'匈牙利语',id:'印度尼西亚语',it:'意大利语',ja:'日语',ko:'韩语',ms:'马来语',nb:'挪威语',nl:'荷兰语',pl:'波兰语',ro:'罗马尼亚语',ru:'俄语',sv:'瑞典语',ta:'泰米尔语',te:'泰卢固语',th:'泰语',tl:'菲律宾语',tr:'土耳其语',uk:'乌克兰语',vi:'越南语','zh-TW':'繁体中文'};
  let auth = {}, generation = 0, currentData = null;
  const searchIds = ['resourceFilter', 'contentFilter', 'audienceFilter', 'labelsFilter'];
  function el(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  async function api(url) {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(response.status === 401 ? '请先使用飞书登录' : response.status === 403 ? '请联系管理员开通“短剧付费 TOP100”权限' : '榜单暂时无法读取，请稍后重试');
    return data;
  }
  function options(select, values, selected, label) {
    select.replaceChildren(...values.map(value => { const option = el('option', label(value)); option.value = value; return option; }));
    select.value = selected;
  }
  function renderRows() {
    if (!currentData) return;
    const data = currentData;
    const [resource, content, audience, query] = searchIds.map(id => $(id).value.trim());
    const needle = query.toLocaleLowerCase();
    const filtered = data.list.filter(row =>
      (!resource || String(row.resource_id || '').includes(resource)) &&
      (!content || row.series_key.includes(content)) &&
      (!audience || (row.audience || '待补充') === audience) &&
      (!needle || [...(row.labels || []), ...(row.label_names || []), ...(row.secondary_labels || [])].join(' ').toLocaleLowerCase().includes(needle))
    );
    $('countLabel').textContent = `显示 ${filtered.length} / ${data.list.length} 部短剧`;
    $('rows').replaceChildren();
    for (const row of filtered) {
      const tr = el('tr', '');
      const rank = el('td', '');
      rank.append(el('span', row.rank, 'rank' + (row.rank <= 3 ? ' top' : '')));
      const drama = el('td', '');
      drama.append(el('div', row.name || '剧名待补充', 'drama-name'),
        el('div', '剧 ID：' + row.series_key, 'drama-id'),
        el('div', '资源 ID：' + (row.resource_id || (row.detail_status === 'conflict' ? '存在冲突' : '待补充')), 'drama-id'));
      const audienceCell = el('td', '');
      audienceCell.append(el('span', row.audience || '待补充', 'badge'));
      const themeCell = el('td', '');
      const themes = el('div', '', 'rank-themes');
      for (const label of row.secondary_labels?.length ? row.secondary_labels : ['待补充']) themes.append(el('span', label, 'badge'));
      themeCell.append(themes);
      if (row.labels?.length) {
        const details = el('details', '', 'rank-labels');
        details.append(el('summary', '原始 labels · ' + row.labels.length), el('p', row.labels.join(' · ')));
        themeCell.append(details);
      }
      if (row.detail_status === 'conflict') themeCell.append(el('p', '剧目资源映射存在冲突，暂不展示题材', 'rank-detail-note'));
      tr.append(rank, drama, el('td', names[row.language_code] || row.language_code), audienceCell, themeCell);
      $('rows').append(tr);
    }
    if (!filtered.length) {
      const text = data.list.length ? '当前榜单没有符合搜索条件的短剧，请调整或清空搜索。' :
        data.availability === 'language_empty' ? '该语种当天没有榜单结果' : '该日期榜单尚未采集或尚未产出';
      const tr = el('tr', ''), td = el('td', text, 'empty');
      td.colSpan = 5; tr.append(td); $('rows').append(tr);
    }
  }
  async function load() {
    const stamp = ++generation;
    const day = $('dateFilter').value, lang = $('languageFilter').value || 'en';
    $('notice').hidden = true;
    $('filters').setAttribute('aria-busy', 'true');
    try {
      const {data} = await api('/api/drama-pay-rank/list?' + new URLSearchParams({date: day, language_code: lang}));
      if (stamp !== generation) return;
      currentData = data;
      options($('dateFilter'), ['', ...data.dates], day, value => value || '最新榜单');
      options($('languageFilter'), Array.from(new Set(['en', ...data.languages])), lang, value => (names[value] || value) + ' · ' + value);
      const selectedAudience = $('audienceFilter').value;
      options($('audienceFilter'), Array.from(new Set(['', ...data.list.map(row => row.audience || '待补充'), selectedAudience])), selectedAudience, value => value || '全部受众');
      $('windowLabel').textContent = data.date ? `榜单 ${data.date} · 统计 ${data.window_start} 至 ${data.window_end}（UTC）` : '尚无榜单';
      $('updatedLabel').textContent = data.fetched_at ? '采集时间 ' + new Date(data.fetched_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) + '（北京）' : '';
      renderRows();
      const today = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
      if (data.status.status !== 'ok' || (!day && data.date && data.date < today)) {
        $('notice').textContent = '今日榜单尚未更新成功，当前展示最近可用结果。'; $('notice').hidden = false;
      }
    } catch (error) {
      if (stamp !== generation) return;
      currentData = null;
      $('notice').textContent = error.message; $('notice').hidden = false;
      $('rows').replaceChildren(); $('windowLabel').textContent = '读取失败';
      $('countLabel').textContent = ''; $('updatedLabel').textContent = '';
    } finally {
      if (stamp === generation) $('filters').setAttribute('aria-busy', 'false');
    }
  }
  $('filters').addEventListener('submit', event => { event.preventDefault(); load(); });
  $('resetSearch').addEventListener('click', () => { searchIds.forEach(id => { $(id).value = ''; }); renderRows(); });
  $('loginBtn').addEventListener('click', () => UiTopbar.handleAuthAction({auth}));
  async function init() {
    try {
      const top = await api('/api/ui/topbar'); auth = top.auth || top;
      UiTopbar.render({auth, userCard: '#userCard', authButton: '#loginBtn', refreshButton: '#refreshBtn'});
      await QuickNav.render({container: '#quickNav', activeKey: 'dramaPayRank', auth});
      await load();
    } catch (error) { $('notice').textContent = error.message; $('notice').hidden = false; }
  }
  init();
})();
