(() => {
  'use strict';
  const metrics = {views: '浏览量', likes: '点赞量', comments: '评论量', shares: '分享量', revenue: '收入（USD）'};
  const byId = id => document.getElementById(id);
  const isFB = Boolean(byId('dramaSocialFields'));
  const defaultChannel = isFB ? 'facebook' : 'x';

  function mount() {
    for (const level of ['drama', 'material']) {
      const sort = byId(level + 'SortBy');
      if (!sort || byId(level + 'XEffects')) continue;
      const box = document.createElement('fieldset');
      box.id = level + 'XEffects';
      box.style.cssText = 'border:1px solid #dbe4ef;border-radius:12px;padding:12px 16px;margin:12px 0;min-width:0';
      const title = document.createElement('legend');
      title.textContent = (level === 'drama' ? '短剧' : '素材') + '社媒效果';
      const label = document.createElement('label');
      label.className = 'field';
      label.textContent = '效果数据来源';
      const selector = document.createElement('select');
      selector.id = level + 'EffectChannel';
      for (const [id, name] of [['x', 'X'], ['facebook', 'Facebook'], ['all', 'X + Facebook 合计']]) {
        selector.add(new Option(name, id));
      }
      selector.value = defaultChannel;
      label.append(selector);
      const note = document.createElement('p');
      note.className = 'helper';
      note.textContent = '按所选渠道的累计表现筛选。浏览在 X 取曝光，在 Facebook 取媒体浏览。留空表示不限。';
      box.append(title, label, note);
      const grid = document.createElement('div');
      grid.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,215px),1fr));gap:10px 16px';
      for (const [metric, name] of Object.entries(metrics)) {
        sort.add(new Option('社媒' + name, 'x_' + metric));
        const group = document.createElement('div');
        const text = document.createElement('span');
        text.textContent = name;
        const range = document.createElement('div');
        range.style.cssText = 'display:flex;gap:8px;margin-top:5px';
        for (const [bound, placeholder] of [['min', '最小值'], ['max', '最大值']]) {
          const input = document.createElement('input');
          input.type = 'number';
          input.min = '0';
          input.step = metric === 'revenue' ? '0.01' : '1';
          input.id = level + '_x_' + metric + '_' + bound;
          input.placeholder = placeholder;
          input.setAttribute('aria-label', title.textContent + name + placeholder);
          input.style.cssText = 'width:48%;min-width:0;padding:8px;border:1px solid #ccd6e2;border-radius:8px';
          range.append(input);
        }
        group.append(text, range);
        grid.append(group);
      }
      box.append(grid);
      (byId(level + 'SocialFields') || sort.closest('.rule-card') || sort.parentElement.parentElement).append(box);
    }
  }

  function fill(level, data = {}) {
    mount();
    byId(level + 'EffectChannel').value = data.effect_channel || defaultChannel;
    for (const metric of Object.keys(metrics)) for (const bound of ['min', 'max']) {
      const key = 'x_' + metric + '_' + bound;
      byId(level + '_' + key).value = data[key] ?? '';
    }
  }

  function read(level) {
    const data = {effect_channel: byId(level + 'EffectChannel').value};
    for (const metric of Object.keys(metrics)) for (const bound of ['min', 'max']) {
      const key = 'x_' + metric + '_' + bound;
      data[key] = byId(level + '_' + key).value.trim() || null;
    }
    return data;
  }
  window.XEffectFields = {mount, fill, read, metrics};
  mount();
})();
