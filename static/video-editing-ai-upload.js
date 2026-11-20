/* Hong Kong COS upload transport; no media bytes cross the API server. */
(() => {
  "use strict";
  function md5Base64(input) {
    const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
    const padded = new Uint8Array(Math.ceil((bytes.length + 9) / 64) * 64); padded.set(bytes); padded[bytes.length] = 128;
    const view = new DataView(padded.buffer); const bits = bytes.length * 8;
    view.setUint32(padded.length - 8, bits >>> 0, true); view.setUint32(padded.length - 4, Math.floor(bits / 4294967296), true);
    let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476;
    const shifts = [[7,12,17,22],[5,9,14,20],[4,11,16,23],[6,10,15,21]];
    const constants = Array.from({ length: 64 }, (_, index) => Math.floor(Math.abs(Math.sin(index + 1)) * 4294967296) >>> 0);
    for (let offset = 0; offset < padded.length; offset += 64) {
      let a = h0, b = h1, c = h2, d = h3;
      for (let i = 0; i < 64; i++) {
        let f, g;
        if (i < 16) { f = (b & c) | (~b & d); g = i; }
        else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
        else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
        else { f = c ^ (b | ~d); g = (7 * i) % 16; }
        const sum = (a + f + constants[i] + view.getUint32(offset + 4 * g, true)) >>> 0;
        const shift = shifts[Math.floor(i / 16)][i % 4]; const rotated = (sum << shift) | (sum >>> (32 - shift));
        a = d; d = c; c = b; b = (b + rotated) >>> 0;
      }
      h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    }
    const digest = new Uint8Array(16); const out = new DataView(digest.buffer);
    [h0,h1,h2,h3].forEach((value, index) => out.setUint32(index * 4, value, true));
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'; let encoded = '';
    for (let i = 0; i < digest.length; i += 3) {
      const value = (digest[i] << 16) | ((digest[i + 1] || 0) << 8) | (digest[i + 2] || 0);
      encoded += alphabet[(value >>> 18) & 63] + alphabet[(value >>> 12) & 63] + (i + 1 < 16 ? alphabet[(value >>> 6) & 63] : '=') + (i + 2 < 16 ? alphabet[value & 63] : '=');
    }
    return encoded;
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function put(signature, blob, progress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', signature.url);
      xhr.withCredentials = false;
      for (const [key, value] of Object.entries(signature.headers)) xhr.setRequestHeader(key, value);
      xhr.upload.onprogress = (event) => progress(event.loaded);
      xhr.timeout = 5 * 60 * 1000;
      const failure = () => {
        const error = new Error('上传连接中断，重新选择同一文件可继续上传');
        error.status = xhr.status;
        reject(error);
      };
      xhr.onerror = xhr.ontimeout = xhr.onabort = failure;
      xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve() : failure();
      xhr.send(blob);
    });
  }

  async function upload({file, series, episode, actor, api, onProgress, store = localStorage, transfer = put}) {
    if (!/\.(mp4|mov)$/i.test(file.name) || file.size < 1 || file.size > 1024 ** 3)
      throw new Error('请选择不超过 1 GiB 的 MP4 或 MOV');
    const key = 'ai-editor-cos-upload:' + JSON.stringify([actor, file.name, file.size, file.lastModified, series, episode]);
    let operation;
    try { operation = store.getItem(key); } catch { /* Storage may be unavailable in private mode. */ }
    if (!operation) {
      operation = crypto.randomUUID();
      try { store.setItem(key, operation); } catch { /* Continue with this page's operation. */ }
    }
    onProgress({percent: 0, text: '正在准备上传…'});
    let session = (await api('/uploads', {operation_id: operation, name: file.name, size: file.size, series, episode})).item;
    const started = performance.now(), inflight = new Map();
    let completed = 0, failure, cursor = 0;
    const resumed = new Map(session.uploaded_parts.map((part) => [part.number, part]));
    const resumedBytes = [...resumed.values()].reduce((sum, part) => sum + part.size, 0);
    completed = resumedBytes;
    function progress() {
      const loaded = Math.min(file.size, completed + [...inflight.values()].reduce((a, b) => a + b, 0));
      const elapsed = (performance.now() - started) / 1000;
      const rate = Math.max(0, loaded - resumedBytes) / Math.max(elapsed, .1);
      const remaining = rate > 0 ? Math.ceil((file.size - loaded) / rate) : null;
      const speed = rate >= 1048576 ? (rate / 1048576).toFixed(1) + ' MB/s' : Math.round(rate / 1024) + ' KB/s';
      const eta = remaining === null ? '' : ` · 预计剩余 ${remaining < 60 ? remaining + ' 秒' : Math.ceil(remaining / 60) + ' 分钟'}`;
      onProgress({percent: 100 * loaded / file.size,
        text: `已上传 ${(loaded / 1048576).toFixed(1)} / ${(file.size / 1048576).toFixed(1)} MB · ${speed}${eta}`});
    }
    if (session.status === 'uploading') {
      const worker = async () => {
        while (!failure && cursor < session.part_count) {
          const number = ++cursor;
          const blob = file.slice((number - 1) * session.part_size, Math.min(number * session.part_size, file.size));
          try {
            const md5 = md5Base64(await blob.arrayBuffer());
            // Even resumed parts are hashed and checked against the server's
            // frozen digest, so a same-name/size replacement cannot mix files.
            let signature = await api(`/uploads/${session.id}/parts/${number}/sign`, {md5});
            if (resumed.has(number)) continue;
            for (let attempt = 0; ; attempt++) {
              try {
                await transfer(signature, blob, (loaded) => { inflight.set(number, loaded); progress(); });
                completed += blob.size;
                inflight.delete(number);
                progress();
                break;
              } catch (error) {
                inflight.delete(number);
                progress();
                if (attempt >= 2 || (error.status && ![401,403,408,429,500,502,503,504].includes(error.status))) throw error;
                await pause(500 * 2 ** attempt);
                signature = await api(`/uploads/${session.id}/parts/${number}/sign`, {md5});
              }
            }
          } catch (error) { failure = error; }
        }
      };
      await Promise.all(Array.from({length: 3}, worker));
      if (failure) throw failure;
    }
    if (session.status !== 'ready') {
      onProgress({percent: 100, text: '文件已上传，正在校验视频…'});
      session = (await api(`/uploads/${session.id}/complete`, {})).item;
      const deadline = Date.now() + 30 * 60 * 1000;
      while (['queued', 'verifying'].includes(session.status)) {
        if (Date.now() > deadline) throw new Error('文件已保存，校验仍在进行；稍后查询媒资库，或选择同一文件查看进度');
        await pause(2000);
        session = (await api(`/uploads/${session.id}`)).item;
      }
    }
    if (session.status !== 'ready') throw new Error(session.error || '视频校验未完成，重新选择同一文件可继续');
    onProgress({percent: 100, text: '上传完成'});
    return session.asset;
  }

  const model = {md5Base64, upload};
  if (typeof module !== 'undefined' && module.exports) module.exports = model;
  else window.AIEditorUpload = model;
})();
