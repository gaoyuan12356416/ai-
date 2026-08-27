// Delayed fake-API checks only: no browser, OAuth, network or production writes.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const functions = [
  "selectableMaterials", "newYoutubeOperationId", "selectedYoutubeChannel",
  "syncYoutubeCommentEligibility", "isCurrentYoutubePublish", "loadYoutubeChannels",
  "closeYoutubePublish", "openYoutubePublish", "submitYoutubePublish",
];

class Element {
  constructor(tag = "div", hidden = false) {
    this.tag = tag;
    this.children = [];
    this.selectedIndex = -1;
    this.disabled = false;
    this.dataset = {};
    this.attributes = {};
    this.events = {};
    this.textContent = "";
    this._value = "";
    const classes = new Set(hidden ? ["hidden"] : []);
    this.classList = {
      add: value => classes.add(value), remove: value => classes.delete(value),
      contains: value => classes.has(value),
    };
  }
  get value() {
    return this.tag === "select" ? this.children[this.selectedIndex]?.value || "" : this._value;
  }
  set value(value) {
    if (this.tag === "select") this.selectedIndex = this.children.findIndex(item => item.value === value);
    else this._value = value;
  }
  get selectedOptions() { return this.children[this.selectedIndex] ? [this.children[this.selectedIndex]] : []; }
  replaceChildren() { this.children = []; this.selectedIndex = -1; }
  appendChild(item) {
    this.children.push(item);
    if (this.tag === "select" && this.selectedIndex < 0) this.selectedIndex = 0;
    return item;
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(event, callback) { this.events[event] = callback; }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function flush() {
  // Drain nested host/VM promise continuations without timers or real I/O.
  for (let index = 0; index < 24; index++) await Promise.resolve();
}

function harness(source) {
  const state = {
    youtubePublishJob: null, youtubeOperationId: "", youtubePublishRequestId: 0,
    youtubePublishLoading: false, youtubeChannelsRequest: null,
  };
  const els = {};
  for (const name of ["youtubePublishModal", "youtubePublishClose", "youtubeChannelLoading",
    "youtubePublishError", "youtubePublishSubmit", "youtubeCommentHint"]) {
    els[name] = new Element("div", ["youtubePublishModal", "youtubeChannelLoading", "youtubePublishError"].includes(name));
  }
  for (const name of ["youtubeChannel", "youtubeSourceKind"]) els[name] = new Element("select");
  for (const name of ["youtubeTitle", "youtubeDescription", "youtubeComment"]) els[name] = new Element("input");
  const requests = [], toasts = [], viewed = [], confirms = [];
  let activeChannels = 0, maxActiveChannels = 0, operation = 0;
  const context = vm.createContext({
    state, els, TextEncoder,
    document: { createElement: tag => new Element(tag) },
    window: {
      crypto: { randomUUID: () => "operation-" + (++operation) },
      confirm: text => { confirms.push(text); return true; },
    },
    api: (url, options) => {
      const kind = url.includes("/youtube/channels?") ? "channels" : options?.method === "POST" ? "post" : "job";
      const request = { ...deferred(), url, options, kind };
      requests.push(request);
      if (kind === "channels") maxActiveChannels = Math.max(maxActiveChannels, ++activeChannels);
      return request.promise.finally(() => { if (kind === "channels") activeChannels--; });
    },
    errorText: error => error.message,
    showError: () => assert.fail("Preparation errors must remain in the current modal"),
    toast: value => toasts.push(value),
    viewJob: async id => viewed.push(id),
  });
  const snippets = functions.map(name => {
    const match = source.match(new RegExp("(?:async )?function " + name + "\\([^]*?\\n    \\}"));
    assert.ok(match, name);
    return match[0];
  });
  const bindings = source.match(/      els\.youtubePublishClose\.onclick[^]*?      els\.youtubePublishSubmit\.onclick[^\n]+/);
  assert.ok(bindings);
  vm.runInContext(snippets.join("\n") + "\n" + bindings[0]
    + '\nels.youtubeChannel.addEventListener("change", syncYoutubeCommentEligibility);', context);
  return {
    state, els, context, requests, toasts, viewed, confirms,
    requestsOf: kind => requests.filter(request => request.kind === kind),
    request(kind, index = 0) {
      const request = this.requestsOf(kind)[index];
      assert.ok(request, "Missing " + kind + " request " + index);
      return request;
    },
    maxActiveChannels: () => maxActiveChannels,
  };
}

const job = (id = "a", app = "1479") => ({
  job_id: id.repeat(32), app_id: app, status: "done", drama_name: "任务 " + id,
  output_video_url: "https://example.test/" + id + ".mp4", content_id: "drama-" + id,
});
const channels = (name = "频道甲", comment = true) => ({ items: [{
  channel_local_id: "263", channel_id: "UC" + "a".repeat(22), channel_name: name,
  youtube_account_id: "255", upload_eligible: true, identity_eligible: true,
  comment_eligible: comment,
}] });
const visible = element => !element.classList.contains("hidden");
function loading(h) {
  assert.ok(visible(h.els.youtubePublishModal));
  assert.ok(visible(h.els.youtubeChannelLoading));
  assert.equal(h.els.youtubePublishModal.attributes["aria-busy"], "true");
  assert.equal(h.els.youtubeChannel.disabled, true);
  assert.equal(h.els.youtubePublishSubmit.disabled, true);
  assert.equal(h.state.youtubePublishLoading, true);
  assert.equal(h.state.youtubePublishJob, null);
}
function failed(h, message) {
  assert.ok(visible(h.els.youtubePublishModal));
  assert.ok(visible(h.els.youtubePublishError));
  assert.ok(h.els.youtubePublishError.textContent.includes(message));
  assert.equal(visible(h.els.youtubeChannelLoading), false);
  assert.equal(h.els.youtubePublishModal.attributes["aria-busy"], "false");
  assert.equal(h.els.youtubeChannel.disabled, true);
  assert.equal(h.els.youtubePublishSubmit.disabled, true);
  assert.equal(h.els.youtubeChannel.children.length, 0);
  assert.equal(h.state.youtubePublishJob, null);
}
async function startChannels(h, item = job()) {
  const run = h.context.openYoutubePublish(item.job_id);
  h.requestsOf("job").at(-1).resolve(item);
  await flush();
  return { run };
}
async function ready(h, item = job(), response = channels()) {
  const { run } = await startChannels(h, item);
  h.requestsOf("channels").at(-1).resolve(response);
  await run;
}
function form(h) {
  h.els.youtubeTitle.value = "标题";
  h.els.youtubeDescription.value = "说明 {{url}}";
  h.els.youtubeComment.value = "一条评论";
}

const cases = [
  ["opens before either API resolves and preserves eligibility on success", async h => {
    const run = h.context.openYoutubePublish(job().job_id);
    loading(h);
    assert.equal(h.requests.length, 1);
    await h.context.submitYoutubePublish();
    assert.equal(h.requestsOf("post").length, 0);
    h.request("job").resolve(job());
    await flush();
    loading(h);
    assert.equal(h.requests.length, 2);
    await h.context.submitYoutubePublish();
    assert.equal(h.requestsOf("post").length, 0);
    h.request("channels").resolve({ items: [channels("无评论频道", false).items[0], channels("可评论频道").items[0]] });
    await run;
    assert.ok(visible(h.els.youtubePublishModal));
    assert.equal(visible(h.els.youtubeChannelLoading), false);
    assert.equal(visible(h.els.youtubePublishError), false);
    assert.equal(h.els.youtubeChannel.disabled, false);
    assert.equal(h.els.youtubePublishSubmit.disabled, false);
    assert.equal(h.els.youtubeChannel.children.length, 2);
    assert.equal(h.els.youtubeChannel.children[0].textContent, "无评论频道（不可评论）");
    assert.equal(h.els.youtubeComment.disabled, true);
    h.els.youtubeChannel.selectedIndex = 1;
    h.els.youtubeChannel.events.change();
    assert.equal(h.els.youtubeComment.disabled, false);
    assert.equal(h.state.youtubePublishJob.job_id, job().job_id);
    assert.equal(h.state.youtubeOperationId, "yt:operation-1");
    assert.equal(h.els.youtubeSourceKind.value, "concat_video");
    assert.equal(h.els.youtubeTitle.value, job().drama_name);
  }],
  ["job read failure stays visible and never requests channels", async h => {
    const run = h.context.openYoutubePublish(job().job_id);
    h.request("job").reject(new Error("任务读取失败，请稍后重试"));
    await run;
    failed(h, "任务读取失败，请稍后重试");
    assert.equal(h.requestsOf("channels").length, 0);
    await h.context.submitYoutubePublish();
    assert.equal(h.requestsOf("post").length, 0);
  }],
  ["channel failure is readable text and cannot submit", async h => {
    const { run } = await startChannels(h);
    h.request("channels").reject(new Error("频道读取失败 <b>请重试</b>"));
    await run;
    failed(h, "频道读取失败 <b>请重试</b>");
    assert.equal(h.els.youtubePublishError.innerHTML, undefined);
    await h.context.submitYoutubePublish();
    assert.equal(h.requestsOf("post").length, 0);
  }],
  ["no available channel cannot submit", async h => {
    const { run } = await startChannels(h);
    h.request("channels").resolve({ items: [] });
    await run;
    failed(h, "当前产品没有可用的 YouTube 上传频道");
    await h.context.submitYoutubePublish();
    assert.equal(h.requestsOf("post").length, 0);
  }],
  ["cover-only material fails before channel lookup", async h => {
    const run = h.context.openYoutubePublish(job().job_id);
    h.request("job").resolve({ ...job(), output_video_url: "", cover_16x9_url: "https://example.test/cover.jpg" });
    await run;
    failed(h, "无可用视频产物");
    assert.equal(h.requestsOf("channels").length, 0);
  }],
  ["closing before job result suppresses channel request", async h => {
    const run = h.context.openYoutubePublish(job().job_id);
    h.els.youtubePublishClose.onclick();
    h.request("job").resolve(job());
    await run;
    assert.equal(visible(h.els.youtubePublishModal), false);
    assert.equal(h.requestsOf("channels").length, 0);
    assert.equal(h.state.youtubePublishJob, null);
  }],
  ["backdrop close ignores late channel success", async h => {
    const { run } = await startChannels(h);
    h.els.youtubePublishModal.events.click({ target: h.els.youtubeTitle });
    assert.ok(visible(h.els.youtubePublishModal));
    h.els.youtubePublishModal.events.click({ target: h.els.youtubePublishModal });
    h.request("channels").resolve(channels());
    await run;
    assert.equal(visible(h.els.youtubePublishModal), false);
    assert.equal(h.els.youtubeChannel.children.length, 0);
    assert.equal(h.els.youtubePublishSubmit.disabled, true);
    assert.equal(h.state.youtubePublishJob, null);
  }],
  ["closed modal ignores late channel failure", async h => {
    const { run } = await startChannels(h);
    h.els.youtubePublishClose.onclick();
    h.request("channels").reject(new Error("迟到错误"));
    await run;
    assert.equal(visible(h.els.youtubePublishModal), false);
    assert.equal(visible(h.els.youtubePublishError), false);
    assert.equal(h.els.youtubePublishError.textContent, "");
  }],
  ["late old job cannot overwrite a newer ready task", async h => {
    const old = h.context.openYoutubePublish(job().job_id);
    await ready(h, job("b", "1480"), channels("频道乙"));
    h.request("job").resolve(job());
    await old;
    assert.equal(h.requestsOf("channels").length, 1);
    assert.equal(h.state.youtubePublishJob.job_id, job("b").job_id);
    assert.equal(h.els.youtubeChannel.children[0].textContent, "频道乙");
  }],
  ["same-product reopen reuses one in-flight channel request", async h => {
    const old = await startChannels(h);
    h.els.youtubePublishClose.onclick();
    const current = await startChannels(h, job("b"));
    loading(h);
    assert.equal(h.requestsOf("channels").length, 1);
    h.request("channels").resolve(channels());
    await Promise.all([old.run, current.run]);
    assert.equal(h.state.youtubePublishJob.job_id, job("b").job_id);
    assert.equal(h.els.youtubeTitle.value, "任务 b");
    assert.equal(h.els.youtubeChannel.children.length, 1);
    assert.equal(h.maxActiveChannels(), 1);
  }],
  ["different-product reopen serializes channel requests", async h => {
    const old = await startChannels(h);
    const current = await startChannels(h, job("b", "1480"));
    assert.equal(h.requestsOf("channels").length, 1);
    h.request("channels").resolve(channels("旧频道"));
    await old.run;
    await flush();
    loading(h);
    assert.equal(h.els.youtubeTitle.value, "任务 b");
    assert.equal(h.els.youtubeChannel.children.length, 0);
    assert.equal(h.request("channels", 1).url, "/api/drama-material/youtube/channels?app_id=1480");
    h.request("channels", 1).resolve(channels("新频道"));
    await current.run;
    assert.equal(h.els.youtubeChannel.children[0].textContent, "新频道");
    assert.equal(h.maxActiveChannels(), 1);
  }],
  ["superseded waiting product is skipped after old request fails", async h => {
    const first = await startChannels(h);
    const skipped = await startChannels(h, job("b", "1480"));
    const current = await startChannels(h, job("c", "1481"));
    h.request("channels").reject(new Error("旧产品错误"));
    await Promise.all([first.run, skipped.run]);
    await flush();
    loading(h);
    assert.equal(visible(h.els.youtubePublishError), false);
    assert.equal(h.requestsOf("channels").length, 2);
    assert.equal(h.request("channels", 1).url, "/api/drama-material/youtube/channels?app_id=1481");
    h.request("channels", 1).resolve(channels("频道丙"));
    await current.run;
    assert.equal(h.state.youtubePublishJob.job_id, job("c").job_id);
    assert.equal(h.maxActiveChannels(), 1);
  }],
  ["reopen after completion reads fresh channels rather than caching", async h => {
    await ready(h);
    h.els.youtubePublishClose.onclick();
    await ready(h, job("b"), channels("新授权频道"));
    assert.equal(h.requestsOf("channels").length, 2);
    assert.equal(h.els.youtubeChannel.children[0].textContent, "新授权频道");
    assert.equal(h.maxActiveChannels(), 1);
  }],
  ["opening a new task immediately clears the old ready identity and inputs", async h => {
    await ready(h);
    form(h);
    const current = h.context.openYoutubePublish(job("b").job_id);
    loading(h);
    assert.equal(h.state.youtubeOperationId, "");
    assert.equal(h.els.youtubeChannel.children.length, 0);
    assert.equal(h.els.youtubeSourceKind.children.length, 0);
    assert.equal(h.els.youtubeTitle.value, "");
    assert.equal(h.els.youtubeDescription.value, "");
    assert.equal(h.els.youtubeComment.value, "");
    assert.equal(h.els.youtubeComment.disabled, true);
    await h.context.submitYoutubePublish();
    assert.equal(h.requestsOf("post").length, 0);
    h.els.youtubePublishClose.onclick();
    h.request("job", 1).resolve(job("b"));
    await current;
    assert.equal(h.requestsOf("channels").length, 1);
  }],
  ["closing a waiting product prevents its deferred channel request", async h => {
    const first = await startChannels(h);
    const waiting = await startChannels(h, job("b", "1480"));
    h.els.youtubePublishClose.onclick();
    h.request("channels").resolve(channels());
    await Promise.all([first.run, waiting.run]);
    assert.equal(visible(h.els.youtubePublishModal), false);
    assert.equal(h.requestsOf("channels").length, 1);
    assert.equal(h.state.youtubePublishJob, null);
    assert.equal(h.toasts.length, 0);
  }],
  ["same-product shared failure affects only the current modal without retry", async h => {
    const first = await startChannels(h);
    const current = await startChannels(h, job("b"));
    h.request("channels").reject(new Error("频道暂不可用"));
    await Promise.all([first.run, current.run]);
    failed(h, "频道暂不可用");
    assert.equal(h.els.youtubeTitle.value, "任务 b");
    assert.equal(h.requestsOf("channels").length, 1);
    assert.equal(h.maxActiveChannels(), 1);
  }],
  ["late submit success cannot close or enable a newly loading modal", async h => {
    await ready(h);
    form(h);
    const submit = h.context.submitYoutubePublish();
    h.els.youtubePublishClose.onclick();
    const current = await startChannels(h, job("b"));
    h.request("post").resolve({ video_state: "queued", comment_status: "queued" });
    await submit;
    loading(h);
    assert.equal(h.toasts.length, 0);
    assert.equal(h.viewed.length, 0);
    h.request("channels", 1).resolve(channels());
    await current.run;
  }],
  ["late duplicate response cannot confirm or submit the newer task", async h => {
    await ready(h);
    form(h);
    const submit = h.context.submitYoutubePublish();
    const current = await startChannels(h, job("b"));
    h.request("post").reject(Object.assign(new Error("旧任务重复"), { code: "youtube_duplicate_confirmation_required" }));
    await submit;
    loading(h);
    assert.equal(h.confirms.length, 0);
    assert.equal(visible(h.els.youtubePublishError), false);
    assert.equal(h.requestsOf("post").length, 1);
    h.request("channels", 1).resolve(channels());
    await current.run;
  }],
  ["explicit duplicate confirmation preserves operation and POST payload", async h => {
    await ready(h);
    form(h);
    const operation = h.state.youtubeOperationId;
    const submit = h.context.submitYoutubePublish();
    const payload = JSON.parse(h.request("post").options.body);
    assert.deepEqual(payload, {
      operation_id: operation, app_id: "1479", channel_local_id: "263",
      channel_id: channels().items[0].channel_id, youtube_account_id: "255",
      material_kind: "concat_video", title: "标题", description_template: "说明 {{url}}",
      comment_text: "一条评论", duplicate_confirmed: false,
    });
    assert.equal(h.request("post").url, "/api/drama-material/jobs/" + job().job_id + "/youtube-publishes");
    h.request("post").reject(Object.assign(new Error("需要确认"), { code: "youtube_duplicate_confirmation_required" }));
    await flush();
    assert.equal(h.confirms.length, 1);
    assert.equal(h.els.youtubePublishSubmit.disabled, true);
    assert.deepEqual(JSON.parse(h.request("post", 1).options.body), { ...payload, duplicate_confirmed: true });
    h.request("post", 1).resolve({ video_state: "queued", comment_status: "queued" });
    await submit;
    assert.equal(visible(h.els.youtubePublishModal), false);
    assert.equal(h.toasts.length, 1);
    assert.deepEqual(h.viewed, [job().job_id]);
    assert.equal(h.requestsOf("channels").length, 1);
  }],
];

async function main() {
  let checks = 0, inlineScripts = 0;
  for (const name of ["index.html", "drama-synthesis.html"]) {
    const source = fs.readFileSync(path.join(root, "static", name), "utf8");
    for (const script of source.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
      new vm.Script(script[1], { filename: name });
      inlineScripts++;
    }
    for (const field of ["youtubePublishRequestId: 0", "youtubePublishLoading: false", "youtubeChannelsRequest: null"])
      assert.ok(source.includes(field), field);
    assert.match(source, /id="youtubeChannelLoading"[^>]+role="status"[^>]+aria-live="polite"/);
    assert.ok(source.includes('youtubeChannelLoading: document.getElementById("youtubeChannelLoading")'));
    for (const [label, test] of cases) {
      let timer;
      try {
        await Promise.race([
          test(harness(source)),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error("Fake API promise did not settle")), 5000);
          }),
        ]);
      }
      catch (error) { error.message = name + " / " + label + ": " + error.message; throw error; }
      finally { clearTimeout(timer); }
      checks++;
    }
  }
  console.log(JSON.stringify({ ok: true, checks, pages: 2, inline_scripts_checked: inlineScripts,
    browser_calls: 0, network_calls: 0, oauth_calls: 0 }));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
