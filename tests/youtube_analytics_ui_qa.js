/* Local static server: python -m http.server 8907 --bind 127.0.0.1 --directory static
 * playwright-cli -s=youtube-analytics run-code --filename tests/youtube_analytics_ui_qa.js
 * Explicitly mocked analytics only. No production reads, writes, or publication. */
async (page) => {
  const origin = 'http://127.0.0.1:8907',checks = [],errors = [],queries = [],exports = [];
  const check = (name,ok) => { if (!ok) throw new Error(name); checks.push(name); };
  const reply = (route,data,status = 200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
  const metrics = {clicks:12480,views:18200,installs:1936,conversions:218,revenue:3264.75,refunds:42.50,install_rate:15.5128,pay_rate:11.2603,links:34};
  const optionData = {owner:[{value:'person-a',label:'Mock 生成人 A'},{value:'person-b',label:'Mock 生成人 B'}],drama:[{value:'drama-001',label:'Mock The Last Promise'},{value:'drama-002',label:'Mock The Hidden Heiress'}],channel:[{value:'channel-001',label:'Mock DramaWave English'},{value:'channel-002',label:'Mock DramaWave Stories'}],link_type:[{value:'auto',label:'自动发布'},{value:'manual',label:'手动短链'}],language:[{value:'en',label:'en'},{value:'es',label:'es'}]};
  let mode = 'normal',scope = 'tenant',held = null,exportMode = 'normal';
  function payload(query) {
    const groups = query.get('group_by').split(','),empty = mode === 'empty',missing = mode === 'missing';
    const start = query.get('start'),end = query.get('end'),dates = []; let date = new Date(start+'T00:00:00Z');
    while (date.toISOString().slice(0,10) <= end && dates.length < 366) { dates.push(date.toISOString().slice(0,10)); date.setUTCDate(date.getUTCDate()+1); }
    const totals = missing ? Object.fromEntries(Object.keys(metrics).map(key => [key,null])) : empty ? {...Object.fromEntries(Object.keys(metrics).map(key => [key,0])),install_rate:null,pay_rate:null} : {...metrics};
    const rows = empty || missing ? [] : ['A','B','C','D'].map((name,index) => ({...metrics,clicks:12480-index*2200,owner:'person-'+name,owner_label:'Mock 生成人 '+name,drama:'drama-00'+(index+1),drama_label:index === 3 ? '<img src=x onerror=alert(1)>' : ['Mock The Last Promise','Mock The Hidden Heiress','Mock A Second Chance'][index],channel:'channel-00'+(index+1),channel_label:'Mock DramaWave '+name,link_type:'auto',link_type_label:'自动发布',language:'en',language_label:'en',date:start}));
    return {totals,rows,trend:dates.map((day,index) => ({date:day,...totals,clicks:missing ? null : empty ? 0 : 900+index*90+(index%2)*340})),ranking:rows.map(row => ({...row,label:[row.owner_label,row.drama_label,row.channel_label].join(' / ')})),group_by:groups,pagination:{page:Number(query.get('page')),page_size:Number(query.get('page_size')),total:empty || missing ? 0 : 75,pages:empty || missing ? 0 : 2},quality:{missing_dates:missing ? dates : [],unmatched_campaigns:0,unmatched_totals:{},excluded_campaigns:0},meta:{start,end,timezone:'UTC',currency:'USD',fetched_at:'2026-09-24T03:00:00Z',source_updated_at:'2026-09-24T02:00:00Z',scope,conversion_label:'付费转化数（充值人数）'},warnings:[]};
  }
  await page.unrouteAll({behavior:'ignoreErrors'});
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',async route => {
    const parts = route.request().url().replace(/^https?:\/\/[^/]+/,'').split('?'),path = parts[0];
    const entries = (parts[1] || '').split('&').filter(Boolean).map(pair => pair.split('=').map(value => decodeURIComponent(value.replace(/\+/g,' '))));
    const url = {searchParams:{get:key => entries.find(entry => entry[0] === key)?.[1] ?? null,getAll:key => entries.filter(entry => entry[0] === key).map(entry => entry[1])}};
    if (path === '/api/ui/topbar') return reply(route,{authenticated:true,user:{name:'Mock 报表管理员',role:scope === 'tenant' ? 'admin' : 'user',is_admin:scope === 'tenant',tenant_key:'qa',permissions:{youtube_auto_publish:true}}});
    if (path === '/api/youtube-analytics/options') return reply(route,{options:optionData,scope});
    if (path === '/api/youtube-analytics/report') {
      queries.push(url.searchParams);
      if (mode === 'held') { held = {route,query:url.searchParams}; return; }
      if (mode === '403' || mode === '401') return reply(route,{message:mode === '401' ? '登录已失效' : '暂无报表权限'},Number(mode));
      if (mode === 'failed') return reply(route,{message:'Mock 数据源暂不可用'},503);
      return reply(route,payload(url.searchParams));
    }
    if (path === '/api/youtube-analytics/export.csv') {
      exports.push(url.searchParams);
      if (exportMode === '403') return reply(route,{message:'Mock 导出权限已撤销'},403);
      return route.fulfill({status:200,contentType:'text/csv; charset=utf-8',headers:{'Content-Disposition':'attachment; filename="mock-report.csv"'},body:'\ufeff日期,生成人,点击\n2026-09-23,Mock QA,12480\n'});
    }
    return reply(route,{message:'Unexpected mock route'},404);
  });
  await page.setViewportSize({width:1600,height:1100});
  await page.goto(origin+'/youtube-analytics.html');
  await page.locator('[data-metric="clicks"]').waitFor();
  check('six real API-backed KPIs',await page.locator('.kpi-card').count() === 6 && await page.locator('[data-metric="clicks"]').textContent() === '12,480');
  check('default owner drama channel grouping',queries.at(-1).get('group_by') === 'owner,drama,channel');
  check('admin scope visible',(await page.locator('#scope-label').textContent()).includes('本租户'));
  const end = new Date(); end.setUTCHours(0,0,0,0); end.setUTCDate(end.getUTCDate()-1); const start = new Date(end); start.setUTCDate(start.getUTCDate()-6);
  check('UTC last seven complete days',queries.at(-1).get('start') === start.toISOString().slice(0,10) && queries.at(-1).get('end') === end.toISOString().slice(0,10));
  check('drama ID shown',(await page.locator('#report-body').textContent()).includes('剧 ID：drama-001'));
  check('XSS escaped as text',await page.locator('#report-body img').count() === 0 && (await page.locator('#report-body').textContent()).includes('<img src=x onerror=alert(1)>'));
  check('rates already percentages',(await page.locator('#conversion-chart').textContent()).includes('15.51%'));
  check('trend points have accessible tooltips',await page.locator('#trend-chart circle title').count() === 7);
  check('navigation wired',await page.locator('#quickNav a[href="/youtube-analytics.html"]').count() === 1);
  await page.screenshot({path:'output/playwright/youtube-analytics-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  check('mobile no page overflow',await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path:'output/playwright/youtube-analytics-mobile.png',fullPage:true});
  await page.setViewportSize({width:1600,height:1100});
  await page.locator('.multi-filter[data-dimension="owner"] summary').click();
  await page.locator('input[name="owner"][value="person-a"]').check();
  await page.locator('input[name="owner"][value="person-b"]').check();
  check('unapplied filters disable export',await page.locator('#export-report').isDisabled());
  await page.locator('#report-search').fill('Promise'); await page.locator('#apply-filters').click();
  await page.waitForFunction(() => document.querySelector('[data-metric="clicks"]'));
  check('repeated multi-select and search passed',queries.at(-1).getAll('owner').join(',') === 'person-a,person-b' && queries.at(-1).get('search') === 'Promise');
  check('fourth dimension disabled',await page.locator('[name="group"][value="date"]').isDisabled());
  await page.locator('[name="group"][value="channel"]').uncheck();await page.locator('[name="group"][value="date"]').check();await page.locator('#apply-filters').click();
  await page.waitForFunction(() => !!document.querySelector('[data-sort="date"]'));
  check('group combination changes table',queries.at(-1).get('group_by') === 'owner,drama,date');
  await page.locator('[data-sort="clicks"]').click();await page.waitForFunction(() => document.querySelector('[data-sort="clicks"]')?.closest('th').getAttribute('aria-sort') === 'ascending');
  check('sort changes API query',queries.at(-1).get('direction') === 'asc');
  await page.locator('#next-page').click();await page.waitForFunction(() => document.querySelector('#page-number').textContent === '2 / 2');
  check('pagination uses backend page',queries.at(-1).get('page') === '2');
  await page.locator('#trend-metric').selectOption('revenue');
  check('trend switches metric locally',(await page.locator('#trend-chart svg').getAttribute('aria-label')).includes('收入'));
  await page.locator('#ranking-metric').selectOption('installs');await page.waitForFunction(() => document.querySelector('#ranking-caption').textContent.includes('安装数'));
  check('ranking metric query',queries.at(-1).get('ranking_metric') === 'installs');
  const downloadPromise = page.waitForEvent('download');await page.locator('#export-report').click();const download = await downloadPromise;
  check('server CSV export exact filters',exports.at(-1).get('group_by') === 'owner,drama,date' && exports.at(-1).getAll('owner').length === 2 && download.suggestedFilename().endsWith('.csv'));
  mode = 'missing'; await page.locator('#refreshPage').click(); await page.waitForFunction(() => document.querySelector('#quality-message').textContent.includes('未同步'));
  check('missing source is not zero',await page.locator('[data-metric="clicks"]').textContent() === '—' && (await page.locator('#trend-chart').textContent()).includes('尚未同步'));
  mode = 'empty'; await page.locator('#refreshPage').click(); await page.waitForFunction(() => document.querySelector('[data-metric="clicks"]')?.textContent === '0');
  check('true empty is explicit zero and no rows',(await page.locator('#report-body').textContent()).includes('没有可归属的数据') && (await page.locator('#conversion-chart').textContent()).includes('—'));
  mode = 'failed'; await page.locator('#refreshPage').click(); await page.waitForFunction(() => document.querySelector('#page-message').textContent.includes('暂不可用'));
  check('failure clears values and disables export',await page.locator('[data-metric]').count() === 0 && await page.locator('#export-report').isDisabled());
  mode = 'normal';await page.locator('#refreshPage').click();await page.locator('[data-metric="clicks"]').waitFor();
  mode = 'held';await page.locator('#refreshPage').click();await page.waitForFunction(() => document.querySelector('#apply-filters').textContent.includes('查询'));
  await page.locator('#report-search').fill('Changed during request');mode = 'normal';
  if (held) { await reply(held.route,payload(held.query)).catch(() => {}); held = null; }
  await page.waitForTimeout(100);
  check('stale response cannot show data for edited filters',await page.locator('[data-metric]').count() === 0 && await page.locator('#export-report').isDisabled());
  await page.locator('#apply-filters').click();await page.locator('[data-metric="clicks"]').waitFor();
  exportMode = '403';await page.locator('#export-report').click();await page.waitForFunction(() => document.querySelector('#page-message').textContent.includes('撤销'));
  check('export 403 clears all sensitive report/filter data',await page.locator('[data-metric]').count() === 0 && await page.locator('#dimension-filters input').count() === 0 && await page.locator('#filter-fields').evaluate(node => node.disabled));
  exportMode = 'normal';scope = 'own';await page.locator('#refreshPage').click();await page.locator('[data-metric="clicks"]').waitFor();
  check('ordinary user scope is explicit',(await page.locator('#scope-label').textContent()).includes('仅本人'));
  mode = '401';await page.locator('#refreshPage').click();await page.waitForFunction(() => document.querySelector('#authButton').textContent === '登录');
  check('report 401 clears data and prompts login',await page.locator('[data-metric]').count() === 0 && await page.locator('#export-report').isDisabled());
  mode = 'normal';await page.locator('#refreshPage').click();await page.locator('[data-metric="clicks"]').waitFor();
  mode = '403';await page.locator('#refreshPage').click();await page.waitForFunction(() => document.querySelector('#page-message').textContent.includes('暂无报表权限'));
  check('report 403 clears data',await page.locator('[data-metric]').count() === 0 && await page.locator('#export-report').isDisabled());
  check('no browser runtime errors',errors.length === 0);
  return {passed:checks.length,checks,errors,screenshots:['output/playwright/youtube-analytics-desktop.png','output/playwright/youtube-analytics-mobile.png']};
}
