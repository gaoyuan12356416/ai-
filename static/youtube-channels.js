(() => {
  'use strict';
  const $ = selector => document.querySelector(selector), BASE = '/api/youtube-auto-publish';
  const fields = ['title','description','comment'], names = {title:'标题',description:'描述',comment:'首评'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const time = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) + ' 北京时间' : '—';
  let auth, rows = [], editor = null, serial = 0, timer, polls = 0;
  async function api(path, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path.startsWith('/api/') ? path : BASE + path, {
        credentials:'same-origin',cache:'no-store',...options,signal:controller.signal,
        headers:{Accept:'application/json',...(options.body ? {'Content-Type':'application/json'} : {})}});
      const value = await response.json();
      if (!response.ok) { const error = new Error(value.message || (response.status === 401 ? '请先登录后台' : response.status === 403 ? '暂无 YouTube 访问权限' : '请求失败，请重试')); error.status = response.status; throw error; }
      return value;
    } catch (error) { if (error.name === 'AbortError') throw new Error('请求超时，请重试；保存结果可通过重新加载核对。'); throw error; }
    finally { clearTimeout(timeout); }
  }
  function message(text, error = false) {
    $('#page-message').className = 'note-box ' + (error ? 'note-error' : 'note-blue');
    $('#page-message').textContent = text;
  }
  function renderRows() {
    const query = $('#channel-search').value.trim().toLowerCase();
    const visible = rows.filter(r => (r.name + ' ' + r.channel_id + ' ' + r.id).toLowerCase().includes(query));
    $('#channel-count').textContent = `共 ${rows.length} 个频道${query ? ' · 匹配 ' + visible.length + ' 个' : ''}`;
    $('#channel-table').innerHTML = visible.length ? visible.map(r => {
      const t = r.template, status = {verified:'鉴权通过',checking:'核验中',blocked:'不可发布',unknown:'待核实'}[r.auth_status] || '待核实';
      const url = /^https:\/\/www\.youtube\.com\/channel\/UC[A-Za-z0-9_-]{22}$/.test(r.channel_url || '') ? r.channel_url : '';
      return `<tr><td>${url ? `<a class="channel-name" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(r.name || '未命名频道')} ↗</a>` : `<strong>${esc(r.name || '未命名频道')}</strong>`}<span class="channel-meta">${esc(r.channel_id)}</span></td><td><span class="badge badge-${r.eligible ? 'green' : r.auth_status === 'blocked' ? 'red' : 'amber'}">${status}</span><span class="channel-meta">${esc(r.reason)}</span><span class="channel-meta">${esc(time(r.checked_at))}</span></td><td>${r.comment_eligible ? '已授权' : r.auth_status === 'verified' ? '未授权' : '待核实'}</td><td><div class="template-tags">${t.configured_fields.length ? t.configured_fields.map(f => `<span class="badge badge-blue">${esc(names[f])}</span>`).join('') : '<span class="channel-meta">未配置 · 保留原值</span>'}</div></td><td>${esc(t.updated_by_name || t.updated_by || '—')}<span class="channel-meta">${esc(time(t.updated_at))}</span></td><td><button class="btn btn-secondary btn-sm" data-edit="${esc(r.id)}">编辑模板</button></td></tr>`;
    }).join('') : '<tr><td colspan="6" class="channel-empty">暂无匹配的频道</td></tr>';
  }
  async function loadChannels(force = false, polling = false) {
    clearTimeout(timer); const current = ++serial;
    if (!polling) { polls = 0; message('正在读取频道状态…'); }
    try {
      const result = await api('/channel-list' + (force ? '?refresh=1' : ''));
      if (current !== serial) return;
      if (!Array.isArray(result.channels)) throw new Error('频道列表响应不完整，请重试');
      if (result.error) throw new Error(result.error);
      rows = result.channels; renderRows(); $('#channel-content').classList.remove('hidden');
      if (result.checking && polls++ < 40) { message('正在核验频道授权，模板仍可编辑。'); timer = setTimeout(() => loadChannels(false,true),1500); }
      else if (result.checking) message('核验尚未完成，请稍后刷新频道状态。');
      else $('#page-message').classList.add('hidden');
    } catch (error) { if (current === serial) { message(error.message, true); if (error.status === 401 || error.status === 403) { rows = []; $('#channel-content').classList.add('hidden'); } } }
  }
  function editorMessage(text, error = false) {
    $('#template-message').className = text ? 'template-message note-box ' + (error ? 'note-error' : 'note-blue') : '';
    $('#template-message').textContent = text;
  }
  async function readTemplate(current) {
    const request = ++current.request; current.loading = true;
    $('#template-fields').disabled = true; $('#save-template').disabled = true;
    $('#reload-template').hidden = true; editorMessage('正在读取模板…');
    try {
      const result = await api('/channels/' + encodeURIComponent(current.channel.id) + '/template');
      if (editor !== current || current.request !== request) return;
      if (!result.template || result.template.channel_id !== current.channel.channel_id || fields.some(f => typeof result.template[f + '_template'] !== 'string')) throw new Error('频道身份或模板已变化，请关闭后刷新列表');
      current.template = result.template;
      fields.forEach(f => { $('#template-' + f).value = current.template[f + '_template']; });
      $('#template-fields').disabled = false; $('#save-template').disabled = false;
      editorMessage(''); $('#template-title').focus();
    } catch (error) { if (editor === current) { editorMessage(error.message,true); $('#reload-template').hidden = false; } }
    finally { if (editor === current && current.request === request) current.loading = false; }
  }
  function openEditor(id) {
    const channel = rows.find(r => String(r.id) === String(id)); if (!channel) return;
    editor = {channel, request:0, saving:false, loading:false};
    $('#template-form').reset(); $('#template-channel').textContent = channel.name + ' · ' + channel.channel_id;
    $('#template-dialog').showModal(); void readTemplate(editor);
  }
  function closeEditor() { if (editor?.saving) return; editor = null; $('#template-dialog').close(); }
  async function save(event) {
    event.preventDefault(); const current = editor;
    if (!current || current.loading || current.saving || !current.template) return;
    const payload = {channel_id:current.template.channel_id,version:current.template.version};
    fields.forEach(f => { payload[f + '_template'] = $('#template-' + f).value; });
    current.saving = true; $('#save-template').disabled = true; $('#template-fields').disabled = true; $('#reload-template').disabled = true; $('#close-template').disabled = true;
    editorMessage('正在保存…');
    try {
      await api('/channels/' + encodeURIComponent(current.channel.id) + '/template', {method:'POST',body:JSON.stringify(payload)});
      current.saving = false; closeEditor();
      const toast = document.createElement('div'); toast.className = 'toast toast-success'; toast.textContent = '频道模板已保存，下次选择该频道时自动带入。'; $('#toast-root').replaceChildren(toast); setTimeout(() => toast.remove(),4500);
      void loadChannels();
    } catch (error) { editorMessage(error.message + ' 当前输入已保留。',true); $('#reload-template').hidden = false; }
    finally { current.saving = false; $('#save-template').disabled = false; $('#template-fields').disabled = false; $('#reload-template').disabled = false; $('#close-template').disabled = false; }
  }
  document.querySelectorAll('.template-macros').forEach(node => {
    for (const macro of ['name','desc','url']) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-secondary'; button.textContent = '{' + macro + '}';
      button.addEventListener('click', () => { const input = $('#' + node.dataset.target); input.setRangeText(button.textContent,input.selectionStart,input.selectionEnd,'end'); input.focus(); }); node.appendChild(button);
    }
  });
  $('#channel-search').addEventListener('input',renderRows);
  $('#channel-table').addEventListener('click',event => { const button = event.target.closest('[data-edit]'); if (button) openEditor(button.dataset.edit); });
  $('#close-template').addEventListener('click',closeEditor);
  $('#template-dialog').addEventListener('cancel',event => { event.preventDefault(); closeEditor(); });
  $('#reload-template').addEventListener('click',() => { if (editor && !editor.loading && !editor.saving) void readTemplate(editor); });
  $('#template-form').addEventListener('submit',save);
  $('#refreshPage').addEventListener('click',() => loadChannels(true));
  $('#authButton').addEventListener('click',() => UiTopbar.handleAuthAction({auth,api}));
  window.addEventListener('pagehide',() => { clearTimeout(timer); ++serial; editor = null; });
  async function init() {
    try {
      auth = await api('/api/ui/topbar');
      UiTopbar.render({auth,userCard:'#userCard',authButton:'#authButton',refreshButton:'#refreshPage'});
      void Promise.resolve(QuickNav.render({container:'#quickNav',auth,activeKey:'youtubeChannelList'})).catch(() => {});
      if (!auth.authenticated) { message('请先使用飞书登录后台。'); return; }
      await loadChannels();
    } catch (error) { message(error.message,true); }
  }
  void init();
})();
