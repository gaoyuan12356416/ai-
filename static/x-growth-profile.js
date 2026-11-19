(() => {
  'use strict';
  const themeNames = {ceo_wealth:'豪门总裁', sweet_romance:'甜宠恋爱', marital:'婚恋纠葛', family:'萌宝家庭', revenge:'逆袭复仇', period_intrigue:'古装权谋', fantasy:'奇幻穿越', suspense:'悬疑冒险', growth:'生活成长'};
  const audienceNames = {female:'女性', male:'男性', lgbtq:'LGBTQ+', general:'通用', __unknown__:'未标注'};
  const number = value => value == null ? '待积累' : Number(value).toLocaleString('zh-CN');
  const percent = value => (value * 100).toFixed(0) + '%';
  const themeName = key => themeNames[key] || key;
  const cellSelector = '[data-growth-account]';
  let accounts = new Map(), profiles = {}, status = 'loading', sequence = 0;
  let refreshController;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function showProfile(id) {
    const account = accounts.get(id);
    if (!account) return;
    const profile = profiles[id];
    const dialog = node('dialog', null, 'xtr-dialog');
    dialog.setAttribute('aria-label', '成长画像');
    const head = node('div', null, 'xtr-head');
    const name = account.username ? '@' + account.username : account.display_name || '账号 ' + id;
    const close = node('button', '关闭', 'button small');
    close.type = 'button';
    close.addEventListener('click', () => dialog.close());
    head.append(node('h2', name + ' · 成长画像'), close);
    dialog.append(head);
    const body = node('div');
    body.style.padding = '16px 20px';
    dialog.append(body);
    if (!profile) {
      body.append(node('p', '等待本账号的首批成长数据。新发布的帖子和粉丝快照将用于建立画像。'));
    } else {
      const observed = profile.observations || {};
      const stages = observed.stages || {};
      const metrics = node('div', null, 'xtr-stats');
      metrics.append(node('span', '近 28 天跟踪 ' + number(observed.tracked_posts || 0) + ' 条帖子'),
                     node('span', '近 7 天净增粉 ' + number(profile.followers?.net_7d)),
                     node('span', '前 7 天净增粉 ' + number(profile.followers?.previous_net_7d)));
      body.append(metrics);
      const stageLabels = [['6h', '6 小时'], ['24h', '24 小时'], ['72h', '72 小时'], ['7d', '7 天']];
      body.append(node('p', '已采集的帖子　' + stageLabels
        .map(([key, label]) => label + ' ' + number(stages[key] || 0) + ' 条').join(' · ')));
      body.append(node('p', '已满 72 小时 ' + number(observed.at_least_72h_posts || 0) + ' 条 · '
        + (profile.mature_posts == null ? '等待首次评分' : '纳入评分 ' + number(profile.mature_posts) + ' 条')));
      if (observed.measured_posts_24h > 0) {
        body.append(node('p', '24 小时自然曝光 ' + number(observed.organic_impressions_24h)
          + '（' + number(observed.measured_posts_24h) + ' 条有数据）'));
      }
      if (profile.themes?.length) {
        const scroll = node('div', null, 'xtr-table-scroll');
        const table = node('table', null, 'xtr-table');
        const header = table.createTHead().insertRow();
        for (const title of ['题材', '受众', '产品 / 语言', '有效样本 / 剧数', '表现分', '状态']) {
          const heading = node('th', title);
          heading.scope = 'col';
          header.append(heading);
        }
        const rows = table.createTBody();
        for (const theme of profile.themes) {
          const row = rows.insertRow();
          const product = ({dramawave:'DramaWave', mobo:'Mobo', moboreels:'MoboReels'})[theme.catalog] || theme.catalog;
          for (const value of [themeName(theme.theme), audienceNames[theme.audience] || theme.audience,
            [product, theme.language].filter(Boolean).join(' / '),
            number(theme.effective_posts) + ' / ' + number(theme.dramas), number(theme.score),
            theme.mature ? '已达到样本要求' : '积累中']) {
            row.append(node('td', value));
          }
        }
        scroll.append(table);
        body.append(scroll);
      } else {
        body.append(node('p', '尚无可评分的题材。系统在发布约 72 小时后，用帖子自然曝光、主页点击和转发收藏评估题材。'));
      }
      const policy = profile.policy || {};
      const shares = Object.entries(policy.weights || {}).filter(([, weight]) => weight > 0)
        .map(([key, weight]) => themeName(key) + ' ' + percent(weight));
      shares.push('探索 ' + percent(policy.exploration ?? 1));
      body.append(node('p', '当前选材比例　' + shares.join(' · ')));
      if (profile.computed_at) {
        const date = new Date(profile.computed_at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
        body.append(node('p', '更新于 ' + date + '（北京时间）', 'xtr-note'));
      }
    }
    dialog.addEventListener('close', () => dialog.remove(), {once:true});
    document.body.append(dialog);
    dialog.showModal();
    close.focus();
  }

  function paint(container = document) {
    for (const cell of container.querySelectorAll(cellSelector)) {
      const id = cell.dataset.growthAccount;
      cell.replaceChildren();
      if (status === 'loading') {
        cell.append(node('span', '读取中…', 'xtr-note'));
        continue;
      }
      const button = node('button', null, status === 'failed' ? 'xtr-retry' : 'xtr-open');
      button.type = 'button';
      cell.append(button);
      if (status === 'failed') {
        button.textContent = '读取失败 · 重试';
        button.addEventListener('click', () => refresh([...accounts.values()]));
        continue;
      }
      const profile = profiles[id];
      const top = profile?.themes?.find(theme => theme.mature);
      button.append(node('span', top ? themeName(top.theme) : profile ? '题材样本积累中' : '等待首次采集', 'xtr-context'));
      if (profile) {
        button.append(node('span', '跟踪 ' + number(profile.observations?.tracked_posts || 0)
          + ' 条 · 72 小时采集 ' + number(profile.observations?.stages?.['72h'] || 0) + ' 条', 'xtr-note'));
        button.append(node('span', '纳入评分 ' + number(profile.mature_posts)
          + ' · 周净增粉 ' + number(profile.followers?.net_7d), 'xtr-note'));
      }
      button.append(node('span', '查看成长画像 ↗', 'xtr-more'));
      button.addEventListener('click', () => showProfile(id));
    }
  }

  async function refresh(items) {
    const version = ++sequence;
    accounts = new Map(items.map(item => [String(item.id), item]));
    profiles = {};
    status = 'loading';
    paint();
    refreshController?.abort();
    const controller = new AbortController();
    refreshController = controller;
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch('/api/admin/x-tag-posts/profiles/growth', {
        credentials:'same-origin', cache:'no-store', signal:controller.signal
      });
      if (!response.ok) throw Error('growth_unavailable');
      const payload = await response.json();
      if (version !== sequence) return;
      profiles = payload.items;
      status = 'ready';
    } catch {
      if (version !== sequence) return;
      status = 'failed';
    } finally {
      clearTimeout(timer);
      if (version === sequence) paint();
    }
  }

  window.XGrowthProfile = {paint, refresh};
})();
