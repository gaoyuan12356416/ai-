const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {md5Base64, upload} = require('../static/video-editing-ai-upload.js');

(async () => {
  for (const input of ['', 'abc', 'upload test', '中'.repeat(100), 'x'.repeat(1024 * 128)]) {
    const bytes = Buffer.from(input);
    assert.equal(md5Base64(bytes), crypto.createHash('md5').update(bytes).digest('base64'));
  }
  const bytes = Buffer.from('abcdefghijklm'), chunk = 3;
  const file = {name: 'test.mp4', size: bytes.length, lastModified: 123,
    slice: (a, b) => new Blob([bytes.subarray(a, b)])};
  const memory = new Map(), signed = [], sent = [], progress = [];
  let operation;
  const store = {getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value)};
  const asset = {id: 'asset'};
  const api = async (path, body) => {
    if (path === '/uploads') {
      if (operation) assert.equal(body.operation_id, operation);
      operation = body.operation_id;
      return {item: {id: 'upload', status: 'uploading', part_size: chunk, part_count: 5,
        uploaded_parts: [{number: 1, size: 3, etag: 'done'}]}};
    }
    const number = Number(path.match(/parts\/(\d+)/)?.[1]);
    if (number) {
      const expected = crypto.createHash('md5').update(bytes.subarray((number - 1) * chunk, number * chunk)).digest('base64');
      assert.equal(body.md5, expected);
      signed.push(number);
      return {number};
    }
    assert.equal(path, '/uploads/upload/complete');
    return {item: {status: 'ready', asset}};
  };
  const args = {file, series: '', episode: 0, actor: {tenant_key: 'tenant', user_id: 'user', name: 'old name'}, api, store,
    onProgress: p => progress.push(p), transfer: async (signature, blob, cb) => {
      sent.push(signature.number); cb(blob.size);
    }};
  assert.deepEqual(await upload(args), asset);
  assert.deepEqual(signed.sort(), [1, 2, 3, 4, 5]);
  assert.deepEqual(sent.sort(), [2, 3, 4, 5]);
  assert(progress.every(p => p.percent >= 0 && p.percent <= 100));
  await upload({...args, actor: {...args.actor, name: 'new name', avatar_url: 'changed'}});
  let active = 0, maximum = 0;
  await assert.rejects(upload({...args, transfer: async () => {
    maximum = Math.max(maximum, ++active);
    await new Promise(resolve => setTimeout(resolve, 10)); active--;
    const error = new Error('permanent rejection'); error.status = 400; throw error;
  }}), /permanent rejection/);
  assert.equal(active, 0); // No unfinished PUT survives the failed upload promise.
  assert(maximum <= 3);
  console.log('frontend upload: MD5, resume identity, concurrency, completion and failure passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
