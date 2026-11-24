const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/video-editing-ai-story.js"), "utf8"), {window});
const {orderText} = window.AIEditorStory;

test("an existing creation previews its two orders before the operator saves any edits", () => {
  const assets = [3,2,1].map(episode_number => ({name:"片", series_code:"One · de",episode_number}));
  const session = {assets,story_order_hint:null};
  assert.match(orderText(session,"episode"),/选入 第 3 集 → 第 2 集 → 第 1 集.*成片 第 1 集 → 第 2 集 → 第 3 集/);
  assert.match(orderText(session,"selection"),/成片 第 3 集 → 第 2 集 → 第 1 集/);
  assets[2].series_code="Two · de";
  assert.match(orderText(session,"episode"),/成片 第 3 集 → 第 2 集 → 第 1 集.*沿用选入顺序/);
});

test("verified server provenance takes precedence over display labels", () => {
  const hint={selection:["B","A"],episode:["A","B"],verified:true};
  assert.match(orderText({story_order_hint:hint},"episode"),/选入 B → A.*成片 A → B/);
});
