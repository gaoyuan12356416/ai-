const { test } = require('node:test');
const assert = require('node:assert/strict');
const { describe } = require('../static/video-editing-ai-production.js');
const task = (status, extra = {}) => ({ status, created_at: 100, ...extra });
const done = () => task('completed', { output: { preview_url: '/private/result' } });

test('parent rendering with unclaimed tasks is waiting, never actual rendering or fake percentage', () => {
  const result = describe({ status: 'rendering', tasks: [task('queued')] }, 190);
  assert.equal(result.title, '排队等待制作');
  assert.equal(result.phase, 2);
  assert.equal(result.waitSeconds, 90);
  assert.equal(result.completed, 0);
  assert.equal('percent' in result, false);
});
test('task receipts expose partial success and completion before parent catches up', () => {
  const partial = describe({ status: 'rendering', tasks: [done(), task('running'), task('queued')] });
  assert.equal(partial.phase, 3);
  assert.deepEqual([partial.completed, partial.running, partial.queued], [1, 1, 1]);
  const complete = describe({ status: 'rendering', tasks: [done(), done()] });
  assert.equal(complete.title, '制作完成');
  assert.equal(complete.phase, 4);
});
test('failed items preserve completed outputs, missing output is not advertised as playable', () => {
  const failed = describe({ status: 'failed', tasks: [done(), task('failed', { error_message: '测试失败原因' })] });
  assert.equal(failed.completed, 1);
  assert.equal(failed.failed, 1);
  assert.equal(failed.detail, '测试失败原因');
  const missing = describe({ status: 'completed', tasks: [task('completed')] });
  assert.equal(missing.completed, 0);
  assert.equal(missing.tone, 'error');
});
test('source preparation and superseded batches remain distinct from video completion', () => {
  assert.equal(describe({ status: 'waiting_sources' }).phase, 0);
  assert.equal(describe({ status: 'preparing' }).phase, 1);
  assert.equal(describe({ status: 'needs_attention' }).tone, 'error');
  assert.equal(describe({ status: 'superseded' }).tone, 'muted');
});
