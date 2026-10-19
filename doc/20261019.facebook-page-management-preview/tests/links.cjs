const assert = require('node:assert/strict');
require('../dist/assets/links.js');
const links = globalThis.FBPreviewLinks;
const page = { id: 'demo-page-01', name: '示例 Page & A' };
const material = { id: 'DEMO-1', name: '片段 #1', language: '中文', series: '她的故事', description: '简介', tag: '爱情', contentId: 'demo-content-1' };
const link = links.buildLink({ page, material, taskId: 'demo-task-01', ledgerId: 101, createdMs: 1700000000123 });
const url = new URL(link.longUrl);
assert.equal(url.origin + url.pathname, 'https://www.dramawavew2a.com/ads/101/2310/view');
assert.deepEqual(Object.fromEntries(url.searchParams), {
  c: 'yingliang_post_CLV_VL_facebook_demo-page-01*1700000000none中文*她的故事*爱情*101',
  af_adset: '示例 Page & A', af_adset_id: 'demo-page-01', af_ad: '片段 #1_contentid[demo-content-1]',
  af_ad_id: 'DEMO-1', af_channel: '900001', af_c_id: 'demo-task-01', af_dp: 'demo-content-1'
});
assert.ok(link.longUrl.includes('af_adset=%E7%A4%BA%E4%BE%8B%20Page%20%26%20A'));
assert.equal((link.longUrl.match(/\*/g) || []).length, 4);
assert.equal(links.render('{name}\n{desc}\n{url}', material, link).text, '她的故事\n简介\n' + link.shortUrl);
assert.equal(links.render('x'.repeat(5000), material, link).error, '');
assert.match(links.render('x'.repeat(5001), material, link).error, /5,000/);
assert.match(links.render('中'.repeat(1667), material, link).error, /5,000/);
assert.match(links.render('{desc}', { ...material, description: 'x'.repeat(5001) }, link).error, /5,000/);
for (const value of ['', '  ', '{bad}', '{{name}}', '{name', 'name}', '{name}{', '{ name }', '{name\n}']) assert.ok(links.validateTemplate(value), value);
assert.equal(links.validateTemplate('{name}{desc}{url}'), '');
assert.match(links.render('{desc}', { ...material, description: '' }, link).error, /缺少/);
assert.match(links.render('{url}', material, null).error, /缺少/);
assert.match(links.render('{name}', { ...material, local: true }, null).error, /缺少/);
assert.equal(links.render('纯文本', { local: true }, null).error, '');
assert.equal(links.buildLink({page, material: { ...material, contentId: '' }}), null);
assert.throws(() => links.buildLink({page, material, taskId: 'demo', ledgerId: 1, createdMs: 1, subUserId: 'AIpost'}));
console.log('PASS: landing, 8-field attribution, encoding, macro validation, byte boundary, missing sources and mock identity guard');
