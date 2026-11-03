'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const runtimeSource = fs.readFileSync(path.join(root, 'static/drama-job-runtime.js'), 'utf8');
const uploadSource = fs.readFileSync(path.join(root, 'static/drama-material-source.js'), 'utf8');
const jobId = 'a'.repeat(32);
const variants = [
  ['concat_video', '合集视频', 'output_video_url'],
  ['no_bgm_video', '去 BGM 视频', 'output_video_no_bgm_url'],
  ['random_template', '随机模板视频', 'output_random_template_url'],
];
const jobFixture = () => ({
  job_id: jobId, status: 'done', material_source_uploads: [],
  output_video_url: 'https://media.example/concat.mp4',
  output_video_no_bgm_url: 'https://media.example/no-bgm.mp4',
  output_random_template_url: 'https://media.example/random.mp4',
});
const videoItems = job => variants.map(([kind, label, field]) => [kind, label, job[field]]).filter(item => item[2]);
const records = (job, status) => videoItems(job).map(([kind, , url]) => ({
  material_kind: kind, source_url: url, status,
  ...(status === 'succeeded' ? {material_id: '456789'} : {}),
}));

function runtimeHarness(job) {
  let timer, clock = 20000, reads = 0, allow = true;
  const cleared = [];
  const doc = {visibilityState: 'visible', querySelectorAll: () => []};
  class Clock extends Date { static now() { return clock; } }
  const context = vm.createContext({
    Date: Clock,
    setInterval(callback, interval) { assert.equal(interval, 1000); timer = callback; return 7; },
    clearInterval(id) { cleared.push(id); },
  });
  vm.runInContext(runtimeSource, context, {filename: 'drama-job-runtime.js'});
  const stop = context.DramaJobRuntime.install({
    getJobs: () => [job], refresh: async () => { reads += 1; },
    canRead: () => allow, document: doc,
  });
  return {doc, stop, cleared, get reads() { return reads; },
    setCanRead(value) { allow = value; },
    async tick(delta = 10000) { clock += delta; await timer(); },
  };
}

function uiHarness(overrides = {}) {
  const job = jobFixture();
  const calls = {api: [], choose: [], toast: [], errors: [], refresh: 0, details: []};
  const context = vm.createContext({
    selectableMaterials: videoItems,
    state: {selectedJob: null},
    els: {detailDrawer: {classList: {contains: () => false}}},
    api: async (url, options) => {
      calls.api.push([url, options]);
      return options ? {item: {status: 'queued'}} : job;
    },
    chooseMaterial: async (value, includeCover) => {
      calls.choose.push([value, includeCover]);
      return videoItems(value)[1];
    },
    toast: value => calls.toast.push(value),
    showError: (...args) => calls.errors.push(args),
    loadJobs: async () => { calls.refresh += 1; },
    viewJob: async value => { calls.details.push(value); },
    ...overrides,
  });
  vm.runInContext(uploadSource + '\nglobalThis.subject = {materialUploadActions, materialUploadStatus, uploadSynthesisMaterial};',
    context, {filename: 'drama-material-source.js'});
  return {job, calls, context, subject: context.subject};
}

test('completed synthesis polls every active material upload state and stops after success', async () => {
  const job = jobFixture();
  const runtime = runtimeHarness(job);
  for (const status of ['queued', 'copying', 'submitting', 'verifying', 'unknown_outcome']) {
    job.material_source_uploads = records(job, status);
    const before = runtime.reads;
    await runtime.tick();
    assert.equal(runtime.reads, before + 1, status);
  }
  job.material_source_uploads = records(job, 'succeeded');
  await runtime.tick();
  assert.equal(runtime.reads, 5);
  job.material_source_uploads = records(job, 'failed');
  await runtime.tick();
  assert.equal(runtime.reads, 5);
  runtime.stop();
  assert.deepEqual(runtime.cleared, [7]);
});

test('upload polling retains ten-second throttle, visibility and access checks', async () => {
  const job = jobFixture();
  job.material_source_uploads = records(job, 'queued');
  const runtime = runtimeHarness(job);
  await runtime.tick();
  assert.equal(runtime.reads, 1);
  await runtime.tick(1000);
  assert.equal(runtime.reads, 1);
  runtime.doc.visibilityState = 'hidden';
  await runtime.tick();
  assert.equal(runtime.reads, 1);
  runtime.doc.visibilityState = 'visible';
  runtime.setCanRead(false);
  await runtime.tick();
  assert.equal(runtime.reads, 1);
  runtime.setCanRead(true);
  await runtime.tick();
  assert.equal(runtime.reads, 2);
});

test('legacy active synthesis still polls, completed jobs without uploads do not', async () => {
  const job = jobFixture();
  const runtime = runtimeHarness(job);
  await runtime.tick();
  assert.equal(runtime.reads, 0);
  job.material_source_uploads = null;
  await runtime.tick();
  assert.equal(runtime.reads, 0);
  job.status = 'rendering';
  await runtime.tick();
  assert.equal(runtime.reads, 1);
});

test('upload button appears for completed videos and disappears without usable videos', () => {
  const {job, subject} = uiHarness();
  assert.match(subject.materialUploadActions(job), /上传到自定义素材库/);
  assert.doesNotMatch(subject.materialUploadActions(job), /\bdisabled\b/);
  assert.equal(subject.materialUploadActions({...job, status: 'rendering'}), '');
  assert.equal(subject.materialUploadActions({...job, status: 'failed'}), '');
  assert.equal(subject.materialUploadActions({job_id: jobId, status: 'done', cover_16x9_url: 'https://media.example/image.jpg'}), '');
});

test('all uploaded variants disable button and show formal material IDs; partial uploads stay selectable', () => {
  const {job, subject} = uiHarness();
  job.material_source_uploads = records(job, 'succeeded');
  const complete = subject.materialUploadActions(job);
  assert.match(complete, /已上传自定义素材库/);
  assert.match(complete, /\bdisabled\b/);
  assert.match(complete, /素材 ID 456789/);
  job.material_source_uploads.pop();
  assert.doesNotMatch(subject.materialUploadActions(job), /\bdisabled\b/);
  assert.match(subject.materialUploadActions(job), /上传到自定义素材库/);
});

test('stale output receipts do not mark current output uploaded', () => {
  const {job, subject} = uiHarness();
  job.material_source_uploads = records(job, 'succeeded');
  job.output_video_url = 'https://media.example/new-concat.mp4';
  assert.doesNotMatch(subject.materialUploadActions(job), /\bdisabled\b/);
});

test('status strings escape HTML and unavailable history stays visibly unknown', () => {
  const {job, subject} = uiHarness();
  job.material_source_uploads = records(job, 'failed');
  job.material_source_uploads[0].error = '<img src=x onerror="alert(1)"> & \'test\'';
  const html = subject.materialUploadActions(job);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt; &amp; &#39;test&#39;/);
  assert.match(html, /上传失败/);
  job.material_source_uploads = null;
  assert.match(subject.materialUploadActions(job), /入库状态暂不可用，请刷新核实/);
  assert.equal(subject.materialUploadStatus({status: 'unknown_outcome'}), '入库结果待核实');
});

test('upload uses the chosen video kind only and refreshes list afterwards', async () => {
  const {subject, calls, job} = uiHarness();
  await subject.uploadSynthesisMaterial(jobId);
  assert.equal(calls.choose.length, 1);
  assert.equal(calls.choose[0][0], job);
  assert.equal(calls.choose[0][1], false);
  assert.equal(calls.api.length, 2);
  const [url, options] = calls.api[1];
  assert.equal(url, `/api/drama-material/jobs/${jobId}/material-source`);
  assert.equal(options.method, 'POST');
  assert.deepEqual(JSON.parse(options.body), {material_kind: 'no_bgm_video'});
  assert.deepEqual(calls.toast, ['等待上传']);
  assert.equal(calls.refresh, 1);
  assert.equal(calls.errors.length, 0);
});

test('simultaneous clicks only submit once and show busy state while request is pending', async () => {
  let resolveRead, postCount = 0;
  const job = jobFixture();
  const {subject} = uiHarness({api: (url, options) => {
    if (!options) return new Promise(resolve => { resolveRead = resolve; });
    postCount += 1;
    return Promise.resolve({item: {status: 'queued'}});
  }});
  const first = subject.uploadSynthesisMaterial(jobId);
  await subject.uploadSynthesisMaterial(jobId);
  assert.match(subject.materialUploadActions(job), /正在提交/);
  assert.match(subject.materialUploadActions(job), /\bdisabled\b/);
  resolveRead(job);
  await first;
  assert.equal(postCount, 1);
  assert.doesNotMatch(subject.materialUploadActions(job), /正在提交/);
});

test('cancelled picker never posts and a transport failure asks for readback without a retry', async () => {
  const cancelled = uiHarness({chooseMaterial: async () => null});
  await cancelled.subject.uploadSynthesisMaterial(jobId);
  assert.equal(cancelled.calls.api.length, 1);
  assert.equal(cancelled.calls.refresh, 1);
  let posts = 0;
  const failed = uiHarness({api: async (url, options) => {
    if (!options) return jobFixture();
    posts += 1;
    throw new Error('response lost');
  }});
  await failed.subject.uploadSynthesisMaterial(jobId);
  assert.equal(posts, 1);
  assert.equal(failed.calls.errors.length, 1);
  assert.match(failed.calls.errors[0][0], /刷新查看入库状态/);
  assert.equal(failed.calls.refresh, 1);
});
