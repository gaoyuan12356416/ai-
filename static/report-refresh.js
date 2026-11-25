/* The collection outlives this HTTP request. Resume polling if the tab reloads. */
window.bindReportRefresh = function ({api, allowed, reload, denied}) {
  const button = document.getElementById('fetch-report-data');
  const note = document.getElementById('fetch-report-status');
  if (!button || !note) return () => {};
  let working = false;
  const key = api + '/refresh';
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  function show(text, error = false) {
    note.textContent = text;
    note.classList.toggle('error', error);
    note.hidden = !text;
  }
  function update() {
    button.disabled = working || !allowed();
    button.textContent = working ? '正在抓取…' : '手动抓取数据';
  }
  async function request(url, method) {
    const response = await fetch(url, {method, cache:'no-store', credentials:'same-origin',
      headers:method === 'POST' ? {'X-Report-Refresh':'1'} : {}, signal:AbortSignal.timeout(20000)});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const error = new Error(body.message || '暂时无法读取抓取状态。');
      error.status = response.status;
      throw error;
    }
    return response.json();
  }
  async function follow(after) {
    if (working || !allowed()) return;
    working = true;
    update();
    try {
      show('正在联系采集服务…');
      let result = after === null ? await request(key, 'POST') : {state:'running', after};
      if (result.state === 'running') sessionStorage.setItem(key, result.after);
      let failures = 0;
      while (result.state === 'running') {
        if (!failures) show('正在从数据源抓取，完成后将自动更新报表。');
        await pause(5000);
        try {
          result = await request(key + '?after=' + encodeURIComponent(result.after), 'GET');
          failures = 0;
        } catch (error) {
          if (error.status === 401 || error.status === 403 || ++failures >= 4) throw error;
          show('连接暂时中断，正在重新查询本轮进度…');
        }
      }
      if (result.state !== 'complete' || !result.version) {
        sessionStorage.removeItem(key);
        throw new Error(result.message || '本轮抓取未完成。');
      }
      show('抓取完成，正在读取新版报表…');
      // The reporting replica can briefly serve its previous snapshot during sync.
      for (let attempt = 0; attempt < 20; attempt++) {
        if (!allowed()) return;
        const version = await reload();
        if (version && version >= result.version) {
          sessionStorage.removeItem(key);
          show('抓取完成，已显示最新数据。');
          return;
        }
        if (version === false) throw new Error('抓取已完成，报表读取失败。请点击页面的刷新按钮重试。');
        show('抓取完成，等待读取新版报表…');
        await pause(10000);
      }
      throw new Error('抓取已完成，新版报表暂未显示。请稍后刷新页面。');
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        sessionStorage.removeItem(key);
        denied(error);
        show('');
      } else {
        show(error.message || '暂时无法确认本轮抓取状态。', true);
      }
    } finally {
      working = false;
      update();
    }
  }
  button.addEventListener('click', () => void follow(sessionStorage.getItem(key)));
  let resumeChecked = false;
  return () => {
    update();
    if (!resumeChecked && allowed()) {
      resumeChecked = true;
      const after = sessionStorage.getItem(key);
      if (after !== null) void follow(after);
    }
  };
};
