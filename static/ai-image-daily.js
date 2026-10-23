(() => {
  'use strict';
  const root = 'https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/ai-image-daily/';
  const $ = id => document.getElementById(id);
  let auth = {};
  let allowed = false;
  let generation = 0;
  let loadingTimer;
  function hideReport(message) {
    allowed = false;
    clearTimeout(loadingTimer);
    $('reportPanel').hidden = true;
    $('reportFrame').removeAttribute('src');
    $('openReport').removeAttribute('href');
    $('reportMessage').textContent = message;
    $('reportMessage').hidden = false;
  }
  function showReport() {
    if (!allowed) return;
    clearTimeout(loadingTimer);
    $('reportPanel').hidden = false;
    $('reportMessage').hidden = false;
    $('reportMessage').textContent = '正在打开最新日报…';
    $('openReport').href = root + 'index.html';
    $('reportFrame').src = root + 'index.html';
    loadingTimer = setTimeout(() => {
      if (!allowed) return;
      $('reportMessage').hidden = false;
      $('reportMessage').textContent = '日报加载较慢，可点击“刷新”重试，或使用“独立窗口查看”。';
    }, 20000);
  }
  async function initialize() {
    const current = ++generation;
    hideReport('正在核验报表权限…');
    try {
      const response = await fetch('/api/ui/topbar', {credentials:'same-origin',cache:'no-store'});
      if (!response.ok) throw new Error('登录状态读取失败，请刷新重试。');
      const payload = await response.json();
      if (current !== generation) return;
      auth = payload;
      UiTopbar.render({auth,userCard:'#userCard',authButton:'#authButton'});
      await QuickNav.render({container:'#quickNav',auth,activeKey:'aiImageDaily'});
      if (current !== generation) return;
      if (!auth.authenticated) { hideReport('请先使用飞书登录后台，再查看 AI 图片素材日报。'); return; }
      const user = auth.user || {};
      if (!user.is_admin && !user.permissions?.social_analytics) {
        hideReport('暂无数据报表权限，请联系管理员。'); return;
      }
      allowed = true;
      showReport();
    } catch (error) {
      if (current === generation) hideReport(error.message || '日报初始化失败，请刷新重试。');
    }
  }
  $('reportFrame').addEventListener('load', () => {
    if (!allowed || !$('reportFrame').getAttribute('src')) return;
    clearTimeout(loadingTimer);
    $('reportMessage').hidden = true;
  });
  $('latestReport').addEventListener('click', showReport);
  $('refreshPage').addEventListener('click', initialize);
  $('authButton').addEventListener('click', () => {
    ++generation;
    hideReport('正在处理登录状态…');
    void UiTopbar.handleAuthAction({auth});
  });
  void initialize();
})();
