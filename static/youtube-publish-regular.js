'use strict';
window.createYoutubeRegular = function (ui) {
  const {esc, shell, api, post, render, safeUrl, top, openModal, openTask, uploadCover, toast, upsert} = ui;
  const route = '/regular';
  const refresh = view => { if (ui.isOpen(view)) render(); };
  const count = text => Array.from(text || '').length;
  const languageNames = {en:'英语',es:'西班牙语','zh-tw':'繁体中文','zh-cn':'简体中文',zh:'中文',ja:'日语',
    ko:'韩语',th:'泰语',id:'印尼语',pt:'葡萄牙语',fr:'法语',de:'德语',ru:'俄语',ar:'阿拉伯语',
    tr:'土耳其语',hi:'印地语',vi:'越南语',it:'意大利语',ms:'马来语'};
  function titleLanguage(row) {
    if (row.titleLanguageInfo) return row.titleLanguageInfo;
    const channel = ui.channels().find(c => String(c.id) === row.channel);
    const saved = row.languageChannel === row.channel ? row.channelLanguage : channel?.language;
    for (const [value, source] of [[saved,'channel'],[row.material.language,'material']]) {
      let code = String(value || '').trim().toLowerCase().replaceAll('_','-');
      code = ({'zh-hant':'zh-tw','zh-hans':'zh-cn'})[code] || code;
      code = Object.keys(languageNames).find(key => languageNames[key] === code) || code;
      if (languageNames[code]) return {language:code,language_label:languageNames[code],language_source:source};
    }
    return {language:'en',language_label:'英语',language_source:'default'};
  }
  function invalidateTitles(row, channelChanged = false) {
    row.titleGeneration = (row.titleGeneration || 0) + 1;
    row.candidates = []; row.translationsZh = []; row.titleLanguageInfo = null;
    if (channelChanged && row.selectedTitle === row.title) row.title = '';
    row.selectedTitle = ''; row.selectedTranslation = '';
  }
  const button = (id, action, label, disabled = false, primary = false) =>
    '<button type="button" class="btn ' + (primary ? 'btn-primary' : 'btn-secondary') +
    ' btn-sm" data-regular-action="' + action + '" data-regular-id="' + esc(id) + '"' +
    (disabled ? ' disabled' : '') + '>' + esc(label) + '</button>';

  function rowField(row, name, label, type = 'text', opts = '') {
    const attr = ' data-regular-field="' + name + '" data-regular-id="' + esc(row.id) + '"';
    const control = type === 'textarea'
      ? '<textarea class="textarea" rows="2"' + attr + '>' + esc(row[name]) + '</textarea>'
      : '<input class="input" type="' + type + '"' + attr + ' value="' + esc(row[name]) + '" ' + opts + '>';
    return '<label class="field"><span class="field-label">' + esc(label) + '</span>' + control + '</label>';
  }

  function select(row, name, label, choices) {
    return '<label class="field"><span class="field-label">' + esc(label) + '</span><select class="select" data-regular-field="' +
      name + '" data-regular-id="' + esc(row.id) + '">' + choices.map(choice =>
        '<option value="' + esc(choice[0]) + '"' + (String(row[name]) === String(choice[0]) ? ' selected' : '') +
        (choice[2] ? ' disabled' : '') + '>' + esc(choice[1]) + '</option>').join('') + '</select></label>';
  }

  function card(row) {
    const id = esc(row.id);
    const note = row.error ? '<p class="note-box note-error" role="alert">' + esc(row.error) + '</p>' : '';
    const heading = '<h3>' + esc(row.material.name) + '</h3><small>素材 ' + id + '</small>';
    if (row.taskId) return '<article class="regular-material" data-key="r-' + id + '">' + heading + note +
      '<p>已保存发布准备任务</p>' + button(id, 'review', '查看成片与最终确认', false, true) + '</article>';
    if (!row.info) return '<article class="regular-material" data-key="r-' + id + '">' + heading + note +
      '<p role="status">' + (row.reading ? '正在核对成片尺寸、时长和实际文件…' : '') + '</p>' +
      (!row.reading ? button(id, 'inspect', '重新核对素材') : '') + '</article>';
    const info = row.info, media = info.media;
    const source = safeUrl(info.preview_url);
    const player = source ? '<video class="regular-video" controls preload="metadata" src="' + esc(source) + '"></video>' : '';
    const spec = media.width + ' × ' + media.height + ' · ' + (media.duration_ms / 1000).toFixed(1) + ' 秒' +
      (media.version ? ' · 定稿 ' + media.version : '');
    if (info.shorts) return '<article class="regular-material" data-key="r-' + id + '">' + heading + player +
      '<p>' + esc(spec) + '</p><p class="note-box note-amber">竖屏或方形且不超过 3 分钟。请调整成片后再作为普通视频发布。</p></article>';
    const channels = [['', '请选择频道'], ...ui.channels().map(c => [String(c.id),
      c.name + ' · ' + (c.language_label || languageNames[c.language] || '未设置语种') +
      (c.eligible ? '' : '（' + (c.reason || '暂不可发布') + '）'), !c.eligible])];
    const selected = channels.some(c => c[0] === row.channel);
    if (row.channel && !selected) channels.push([row.channel, '默认频道暂不可用，请重新选择', true]);
    const language = titleLanguage(row);
    const languageHint = !row.channel ? '选择频道后，按频道语言生成标题并附中文翻译。' :
      '标题语言：' + language.language_label + (language.language_source === 'channel' ? '（随频道）' :
        language.language_source === 'material' ? '（频道未设置语种，使用素材语言）' : '（频道和素材未设置可用语种，默认英语）');
    const candidates = row.candidates.map((t, i) => '<div class="regular-candidate"><span><bdi lang="' +
      esc(language.language) + '">' + esc(t) + '</bdi><small class="field-hint" lang="zh-CN">' +
      (row.translationsZh?.[i] ? '中文：' + esc(row.translationsZh[i]) : '中文翻译未返回，可重新生成') +
      '</small></span>' + button(id, 'use-' + i, '用这个') + button(id, 'copy-' + i, '复制') + '</div>').join('');
    const coverInputs = row.coverSource === 'local'
      ? '<label class="field"><span class="field-label">上传 JPG 或 PNG（最多 2 MB）</span><input type="file" accept="image/jpeg,image/png" data-regular-file="' + id + '"></label>' +
        (row.cover ? '<img class="regular-thumb" src="' + esc(row.cover.preview) + '" alt="本地封面预览">' : '')
      : rowField(row, 'requirements', 'AI 封面要求', 'textarea') +
        (safeUrl(row.material.drama_cover_url) ? '<img class="regular-thumb" src="' + esc(row.material.drama_cover_url) + '" alt="原剧参考封面">' : '<p class="field-hint">缺少原剧参考图时，可上传本地封面。</p>');
    return '<article class="regular-material" data-key="r-' + id + '">' + heading + player +
      '<p class="field-hint">' + esc(spec) + '</p>' + note +
      '<fieldset ' + (row.preparing || row.pending ? 'disabled' : '') + '><div class="regular-grid">' +
      select(row, 'channel', '发布频道', channels) + rowField(row, 'time', '北京时间（留空立即发布）', 'datetime-local') + '</div>' +
      '<div class="regular-actions">' + button(id, 'default', '设为本剧默认频道', !row.channel || !row.material.content_id) +
      (row.channel && row.channel === row.defaultChannel ? '<span class="field-hint">本剧默认频道</span>' : '') +
      (row.templateStatus === 'error' ? button(id, 'template', '重读频道模板') : '') + '</div>' +
      select(row, 'mode', '标题写法', [['', '请选择生成依据'], ['clip', '本条成片内容'], ['story', '全剧推广卖点']]) +
      '<p class="field-hint">' + esc(languageHint) + '</p>' +
      (row.mode === 'story' ? rowField(row, 'synopsis', '全剧简介（可修改）', 'textarea') : '') +
      (row.mode === 'clip' ? rowField(row, 'notes', info.clip_evidence ? '片中剧情补充（可选）' : '填写本条视频实际出现的剧情', 'textarea') : '') +
      '<div class="regular-actions">' + button(id, 'titles', row.generating ? '正在生成…' :
        (row.candidates.length ? '重新生成' : '生成') + ' 3 个' + (row.channel ? language.language_label : '') + '标题',
        !row.channel || !row.mode || row.generating) + '</div>' +
      candidates + rowField(row, 'title', '发布标题（可直接填写）', 'text', 'maxlength="100"') +
      (row.selectedTitle === row.title && row.selectedTranslation ? '<p class="field-hint" lang="zh-CN">中文：' + esc(row.selectedTranslation) + '</p>' : '') +
      rowField(row, 'description', '视频描述', 'textarea') + rowField(row, 'comment', '首条评论（可选）', 'textarea') +
      select(row, 'coverSource', '封面来源', [['local', '运营上传'], ['ai', 'AI 参考原剧封面生成']]) +
      coverInputs + '</fieldset><div class="regular-actions">' +
      button(id, 'prepare', row.preparing ? '正在准备…' : row.pending ? '核对原提交结果' : '保存并准备最终预览', row.preparing, true) +
      '</div></article>';
  }

  function view(v) {
    const channels = ui.channelState();
    const status = channels.error || (channels.pollExpired ? '频道鉴权还未完成，可手动刷新。' :
      channels.checking || channels.loading ? '正在检查频道授权，其他内容可以继续填写。' : '');
    const refreshChannels = '<div class="regular-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="retry-channels"' +
      (channels.loading ? ' disabled' : '') + '>刷新频道鉴权</button><span role="status">' + esc(status) + '</span></div>';
    return shell(v, '普通视频发布', '每条视频单独设置频道和时间，封面就绪后确认发布。',
      refreshChannels + v.rows.map(card).join(''), '<button class="btn btn-secondary" data-action="close-modal">关闭</button> ' +
      button('', 'prepare-all', '批量准备预览', false, true), true);
  }

  function review(v, t) {
    const url = safeUrl(t.regular_media.preview_url);
    const preview = safeUrl(t.cover_url);
    v.regularReviewToken = t.review_token;
    const id = t.material.id;
    const media = t.regular_media;
    const revision = media.version ? '定稿 ' + media.version : '文件版本 ' + media.sha256.slice(0,12);
    const info = '<div class="regular-grid"><div><h3>最终成片</h3><video class="regular-video" src="' + esc(url) +
      '" controls preload="metadata"></video><p class="field-hint">素材 ' + esc(id) + ' · ' +
      esc(revision + ' · ' + media.width + ' × ' + media.height + ' · ' + (media.duration_ms / 1000).toFixed(1) + ' 秒') +
      '</p></div><div><h3>最终封面</h3>' +
      (preview ? '<img class="regular-review-cover" src="' + esc(preview) + '" alt="最终封面">' : '') + '</div></div>';
    let edit = '';
    if (v.mode === 'manual') edit = ui.coverInput(v.manualCover, 'manual') +
      '<button class="btn btn-secondary" data-action="confirm-manual"' + (v.manualCover ? '' : ' disabled') + '>保存替换封面</button>';
    if (v.mode === 'reject') edit = '<label class="field">重新生成封面的要求<textarea id="reject-reason" class="textarea">' + esc(v.feedback || '') + '</textarea></label>' +
      '<button class="btn btn-secondary" data-action="confirm-reject">生成新封面</button>';
    if (v.mode === 'regular-title') edit = '<label class="field">修改标题<input id="regular-review-title" class="input" maxlength="100" value="' +
      esc(v.editTitle ?? t.title) + '"></label>' + button(id, 'save-title', '保存标题');
    if (v.mode) edit += ' <button class="btn btn-ghost" data-action="cancel-review-mode">取消修改</button>';
    const summary = '<div class="summary-grid"><div class="summary-item"><span>标题</span><strong>' + esc(t.title) +
      '</strong></div><div class="summary-item"><span>频道</span><strong>' + esc(t.channel.name) +
      '</strong></div><div class="summary-item"><span>发布时间</span><strong>' + esc(t.publish_at ? ui.beijingTime(t.publish_at) : '立即发布') +
      '</strong></div></div>' + (t.description || t.comment ? '<details><summary>描述与首评</summary><p>' + esc(t.description) + '</p><p>' + esc(t.comment) + '</p></details>' : '');
    const controls = '<div class="regular-actions">' + button(id, 'edit-title', '修改标题') +
      ' <button class="btn btn-secondary btn-sm" data-action="manual">更换封面</button> ' +
      (t.cover_source === 'ai' ? '<button class="btn btn-secondary btn-sm" data-action="reject">重做 AI 封面</button>' : '') +
      ' <button class="btn btn-secondary btn-sm" data-action="schedule-reschedule">修改排期</button>' +
      (t.publish_at ? ' <button class="btn btn-secondary btn-sm" data-action="schedule-immediate">改为立即发布</button>' : '') + '</div>';
    return shell(v, '最终发布确认', '确认此版视频、标题、封面、频道和发布时间。', info + summary + controls + edit,
      '<button class="btn btn-secondary" data-action="close-modal">稍后处理</button> <button class="btn btn-primary" data-action="approve"' +
      (!preview || !url || v.mode ? ' disabled' : '') + '>确认' + (t.publish_at ? '定时' : '立即') + '发布</button>', true);
  }

  async function template(v, row) {
    const channel = row.channel, generation = ++row.templateGeneration;
    if (row.templateChannel !== channel) invalidateTitles(row, true);
    row.templateChannel = channel;
    row.templateStatus = channel ? 'loading' : 'ready';
    function set(field, text) { if (!row.dirty.has(field)) row[field] = text; }
    set('description', ui.defaultDescription()); set('comment', ''); set('requirements', '');
    if (!channel) return;
    try {
      const data = await api('/channels/' + encodeURIComponent(channel) + '/template');
      if (row.templateGeneration !== generation || !ui.isOpen(v)) return;
      const value = data.template;
      if (!value) throw new Error('频道模板信息不完整');
      if (row.languageChannel === channel && row.channelLanguage !== value.language) invalidateTitles(row, true);
      row.languageChannel = channel; row.channelLanguage = value.language || '';
      if (value.description_template) set('description', value.description_template);
      if (value.comment_template) set('comment', value.comment_template);
      if (value.cover_requirements_template) set('requirements', value.cover_requirements_template);
      row.templateStatus = 'ready';
    } catch (error) {
      if (row.templateGeneration === generation) { row.templateStatus = 'error'; row.error = error.message; }
    } finally { refresh(v); }
  }

  async function inspect(v, row, force = false) {
    if (row.reading) return;
    row.reading = true; row.error = ''; refresh(v);
    try {
      let poll = false;
      for (;;) {
        if (!ui.isOpen(v)) return;
        const data = await api(route + '/materials/' + row.id + (force ? '?refresh=1' : poll ? '?poll=1' : ''));
        force = false;
        if (!ui.isOpen(v)) return;
        if (data.status === 'ready') {
          row.info = data; row.material = data.material; row.synopsis = data.material.macro_desc || '';
          row.defaultChannel = data.default_channel || '';
          if (!row.dirty.has('channel')) row.channel = row.defaultChannel;
          if (row.channel) void template(v,row);
          return;
        }
        if (data.status !== 'checking') throw new Error('成片校验状态无效，请重试');
        poll = true;
        await new Promise(resolve => setTimeout(resolve, 1800));
      }
    } catch (error) { row.error = error.message; }
    finally { row.reading = false; refresh(v); }
  }

  function start(materials) {
    const rows = materials.map(material => ({id:String(material.id), material, info:null, channel:'', defaultChannel:'',
      mode:'', title:'', synopsis:'', notes:'', candidates:[], translationsZh:[], description:ui.defaultDescription(), comment:'',
      time:'', coverSource:'local', requirements:'', cover:null, dirty:new Set(), templateStatus:'ready',
      templateGeneration:0, operationId:crypto.randomUUID(), pending:null, taskId:'', error:''}));
    openModal('regular', {rows});
    const v = top(); void ui.loadChannels();
    // Keep catalog reads bounded while letting every selected row finish independently.
    const queue = rows.slice();
    async function inspectRows() { while (queue.length && ui.isOpen(v)) await inspect(v,queue.shift()); }
    void Promise.all(Array.from({length:Math.min(3,rows.length)}, inspectRows));
  }

  async function prepare(v,row) {
    if (row.preparing || row.taskId || !row.info || row.info.shorts) return;
    row.preparing = true; row.error = ''; refresh(v);
    try {
      if (!row.pending) {
        if (ui.channelState().error || !ui.channels().some(c => String(c.id) === row.channel && c.eligible)) throw new Error('请为这条素材选择可用频道');
        if (row.templateStatus !== 'ready') throw new Error('请等频道模板读取完成或点击重新读取');
        if (!row.title.trim() || count(row.title) > 100) throw new Error('请填写 1–100 字符的标题');
        if (!row.description.trim()) throw new Error('请填写视频描述');
        const error = ui.scheduleError(row.time);
        if (error) throw new Error(error);
        if (row.coverSource === 'local' && !row.cover) throw new Error('请上传该条视频的封面');
        if (row.coverSource === 'ai' && !row.requirements.trim()) throw new Error('请填写 AI 封面要求');
        const asset = row.coverSource === 'local' ? await uploadCover(row.cover) : null;
        row.pending = {operation_id:row.operationId,material_id:row.id,sha256:row.info.media.sha256,channel_id:row.channel,
          title_template:row.title,description_template:row.description,comment_template:row.comment,
          cover_source:row.coverSource,requirements:row.requirements,cover_asset_id:asset?.id || '',publish_at:ui.publishAtIso(row.time)};
      }
      const result = await post(route + '/tasks',row.pending);
      row.taskId = result.task.id; upsert(result.task,true); row.pending = null; void ui.loadTasks(true);
    } catch (error) {
      row.error = error.message;
      if (!error.uncertain) { row.pending = null; row.operationId = crypto.randomUUID(); }
    } finally { row.preparing = false; refresh(v); }
  }

  async function act(v,row,action) {
    row.error = '';
    if (action === 'inspect') { await inspect(v,row,true); return; }
    if (action === 'review') { await openTask(row.taskId,'review'); return; }
    if (action === 'prepare') { await prepare(v,row); return; }
    if (action === 'template') { await template(v,row); return; }
    if (action === 'default') {
      const result = await post(route + '/default-channel',{material_id:row.id,channel_id:row.channel});
      row.defaultChannel = result.channel_id;
      for (const other of v.rows) {
        if (other === row || other.material.content_id !== row.material.content_id || other.material.app_id !== row.material.app_id) continue;
        other.defaultChannel = result.channel_id;
        if (!other.dirty.has('channel') && !other.taskId && !other.pending) { other.channel=result.channel_id; void template(v,other); }
      }
      toast('本剧默认频道已保存'); return;
    }
    if (action === 'titles') {
      if (row.generating) return;
      if (!row.channel) throw new Error('请先选择发布频道');
      const generation = row.titleGeneration = (row.titleGeneration || 0) + 1;
      row.generating = true; refresh(v);
      try {
        const response = await api(route + '/titles',{method:'POST',timeoutMs:140000,body:JSON.stringify({
          material_id:row.id,sha256:row.info.media.sha256,channel_id:row.channel,
          mode:row.mode,synopsis:row.synopsis,clip_notes:row.notes})});
        if (row.titleGeneration === generation && ui.isOpen(v)) {
          row.candidates = response.titles; row.translationsZh = response.translations_zh || [];
          row.titleLanguageInfo = response;
        }
      } catch (error) {
        if (row.titleGeneration === generation && ui.isOpen(v)) throw error;
      } finally { row.generating = false; }
      return;
    }
    const choice = /^(use|copy)-(\d+)$/.exec(action);
    if (choice && row.candidates[Number(choice[2])]) {
      const title = row.candidates[Number(choice[2])];
      if (choice[1] === 'use') {
        row.title = title; row.selectedTitle = title;
        row.selectedTranslation = row.translationsZh?.[Number(choice[2])] || '';
      }
      else { await navigator.clipboard.writeText(title); toast('已复制标题'); }
    }
  }

  document.addEventListener('click', async event => {
    const el = event.target.closest?.('[data-regular-action]'); if (!el || el.disabled) return;
    const v = top(), action = el.dataset.regularAction;
    if (v?.type === 'review') {
      const t = ui.task(v.id); if (!t?.regular_media) return;
      if (action === 'edit-title') { v.mode='regular-title'; v.editTitle=t.title; render(); }
      if (action === 'save-title') {
        try {
          const data = await post(route + '/tasks/' + t.id + '/title',{title:v.editTitle,review_token:v.regularReviewToken});
          upsert(data.task,true); v.mode=''; v.error='';
        } catch (error) { v.error=error.message; }
        refresh(v);
      }
      return;
    }
    if (v?.type !== 'regular') return;
    if (action === 'prepare-all') {
      const remaining = v.rows.filter(r => !r.taskId);
      async function prepareRows() { while (remaining.length && ui.isOpen(v)) await prepare(v,remaining.shift()); }
      await Promise.all(Array.from({length:Math.min(3,remaining.length)},prepareRows)); return;
    }
    const row = v.rows.find(r => r.id === el.dataset.regularId);
    if (!row) return;
    try { await act(v,row,action); } catch (error) { row.error=error.message; }
    refresh(v);
  });

  document.addEventListener('input', event => {
    const el=event.target,v=top();
    if (el.id==='regular-review-title' && v?.type==='review') { v.editTitle=el.value; return; }
    if (v?.type !== 'regular' || !el.dataset.regularField) return;
    const row = v.rows.find(r => r.id === el.dataset.regularId), field=el.dataset.regularField;
    if (!row || row.pending || row.preparing) return;
    row[field]=el.value; row.dirty.add(field);
    if (['mode','synopsis','notes'].includes(field)) invalidateTitles(row);
    if (field==='title') { row.selectedTitle=''; row.selectedTranslation=''; }
    if (field==='channel') void template(v,row);
    if (['channel','mode','coverSource','title','synopsis','notes'].includes(field)) refresh(v);
  });
  document.addEventListener('change',event=>{
    const el=event.target,v=top();
    if (v?.type!=='regular' || !el.dataset.regularFile) return;
    const row=v.rows.find(r=>r.id===el.dataset.regularFile),file=el.files[0];
    if (!row || !file) return;
    if (!['image/jpeg','image/png'].includes(file.type) || !file.size || file.size>2*1024*1024) {
      row.error='请选择 2 MB 以内的 JPG 或 PNG'; refresh(v); return;
    }
    if (row.cover) URL.revokeObjectURL(row.cover.preview);
    row.cover={file,preview:URL.createObjectURL(file)}; row.error=''; refresh(v);
  });
  return {start,view,review,close(v) { for (const row of v.rows || []) if (row.cover) URL.revokeObjectURL(row.cover.preview); }};
};
