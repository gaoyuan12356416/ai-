/* Ordinary video production uses the same owner-scoped studio, assets and player. */
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);

  function configure({assets, current, openModal, closeModal, esc, action, save}) {
    const byId = new Map(assets.map((a) => [a.id, a]));
    const order = [...new Set([...(current?.body_source_ids || []), ...assets.map((a) => a.id)])].filter(id => byId.has(id));
    const body = new Set(current ? current.body_source_ids : order);
    const hooks = new Set(current?.hook_source_ids || []);
    openModal("YouTube 普通视频", "按列表顺序拼正片。另选的片头素材仅用于提取开场亮点。",
      '<div id="regularSourceRows" class="regular-rows"></div>',
      '<button id="saveRegularConfig" class="primary">使用此剪法</button>', "regular-config");
    const draw = () => {
      el("regularSourceRows").innerHTML = order.map((id, i) => {
        const a = byId.get(id);
        return `<section class="regular-row"><div><strong>${esc(a.name)}</strong>
          <small>${esc(a.series_code || "本地素材")}</small></div><div class="regular-options">
          <label><input type="checkbox" data-role="body" data-i="${i}" ${body.has(id) ? "checked" : ""}>正片</label>
          <label><input type="checkbox" data-role="hook" data-i="${i}" ${hooks.has(id) ? "checked" : ""}>片头备选</label>
          <button data-shift="-1" data-i="${i}" aria-label="上移" ${!i ? "disabled" : ""}>↑</button>
          <button data-shift="1" data-i="${i}" aria-label="下移" ${i === order.length-1 ? "disabled" : ""}>↓</button></div></section>`;
      }).join("");
    };
    el("regularSourceRows").onchange = ({target}) => {
      if (!target.dataset.role) return;
      const chosen = target.dataset.role === "body" ? body : hooks;
      const id = order[Number(target.dataset.i)];
      if (target.checked) chosen.add(id); else chosen.delete(id);
    };
    el("regularSourceRows").onclick = ({target}) => {
      const b = target.closest("button[data-shift]");
      if (!b) return;
      move(order, Number(b.dataset.i), Number(b.dataset.shift));
      draw();
    };
    el("saveRegularConfig").onclick = () => action(async () => {
      const bodyIds = order.filter((s) => body.has(s)), hookIds = order.filter((s) => hooks.has(s));
      if (!bodyIds.length) throw Error("请至少选一条正片");
      if (order.some((s) => !body.has(s) && !hooks.has(s))) throw Error("请为每条选入素材勾选用途，或从媒资库的本次选择中移除");
      await save({body_source_ids: bodyIds, hook_source_ids: hookIds});
      closeModal();
    });
    draw();
  }

  function move(rows, from, step) {
    const to = from + step;
    if (to >= 0 && to < rows.length) [rows[from], rows[to]] = [rows[to], rows[from]];
  }

  async function refine({task, api, openModal, closeModal, action, esc, toast, openOriginal, refresh}) {
    const path = "/youtube-regular/tasks/" + task.id + "/timeline";
    const data = await api(path);
    const clips = data.clips.map((c) => ({...c}));
    const saveOperation = {signature: "", id: ""};
    openModal("精修原片剪点", task.name + " · 版本 " + task.version,
      '<video id="regularPreview" class="regular-preview" controls playsinline preload="metadata" hidden></video>' +
      '<div id="regularClipRows" class="regular-rows"></div>',
      (task.id !== task.original_task_id ? '<button id="regularOriginal">查看 AI 原版</button>' : '') +
      '<button id="submitRegularTrim" class="primary">保存并制作新版</button>', "regular-trim");
    const area = el("regularClipRows"), player = el("regularPreview");
    let playingId = null, previewSeq = 0;
    const redraw = () => {
      area.innerHTML = clips.map((c, i) => {
        const src = data.sources[c.source_id], max = src.duration_ms / 1000;
        return `<section class="regular-row"><div><strong>${i + 1}. ${c.id === "hook" ? "片头 · " : ""}${esc(src.name)}</strong>
          <small>原片 ${max.toFixed(3)} 秒</small></div><div class="regular-options">
          <label>入点（秒）<input type="number" step="0.001" min="0" max="${max}" data-field="start_ms" data-i="${i}" value="${c.start_ms/1000}"></label>
          <label>出点（秒）<input type="number" step="0.001" min="0" max="${max}" data-field="end_ms" data-i="${i}" value="${c.end_ms/1000}"></label>
          <button data-cmd="preview" data-i="${i}">预览这段</button><button data-cmd="up" data-i="${i}" ${i ? "" : "disabled"} aria-label="上移">↑</button>
          <button data-cmd="down" data-i="${i}" ${i === clips.length-1 ? "disabled" : ""} aria-label="下移">↓</button>
          <button data-cmd="remove" data-i="${i}" ${clips.length === 1 ? "disabled" : ""}>删除</button></div></section>`;
      }).join("");
    };
    area.oninput = ({target}) => {
      const field = target.dataset.field;
      if (field) clips[Number(target.dataset.i)][field] = target.value === "" ? null : Math.round(Number(target.value) * 1000);
    };
    const valid = (c) => Number.isInteger(c.start_ms) && Number.isInteger(c.end_ms)
      && c.start_ms >= 0 && c.end_ms <= data.sources[c.source_id].duration_ms
      && Math.round(c.end_ms * 0.03) > Math.round(c.start_ms * 0.03);
    player.ontimeupdate = () => {
      const c = clips.find((clip) => clip.id === playingId);
      if (c && player.currentTime * 1000 >= c.end_ms) player.pause();
    };
    area.onclick = ({target}) => {
      const b = target.closest("[data-cmd]");
      if (!b) return;
      const i = Number(b.dataset.i), c = clips[i], cmd = b.dataset.cmd;
      if (cmd === "preview") {
        if (!valid(c)) return toast("请检查片段的入点和出点", true);
        const seq = ++previewSeq;
        playingId = c.id;
        player.hidden = false;
        player.src = data.sources[c.source_id].preview_url;
        player.onloadedmetadata = () => {
          if (seq !== previewSeq || !player.isConnected) return;
          player.currentTime = c.start_ms / 1000;
          player.play().catch(() => {});
        };
        return;
      }
      if (cmd === "remove" && clips.length > 1) clips.splice(i, 1);
      if (cmd === "up" || cmd === "down") move(clips, i, cmd === "up" ? -1 : 1);
      redraw();
    };
    if (el("regularOriginal")) el("regularOriginal").onclick = () => action(async () => {
      const {item} = await api("/tasks/" + task.original_task_id);
      closeModal(); await openOriginal(item);
    });
    el("submitRegularTrim").onclick = () => action(async () => {
      if (!clips.every(valid)) throw Error("请检查每段的入点和出点，出点须在原片范围内");
      const edits = clips.map(({id, start_ms, end_ms}) => ({id, start_ms, end_ms}));
      const signature = JSON.stringify(edits);
      if (signature !== saveOperation.signature) {
        saveOperation.signature = signature; saveOperation.id = crypto.randomUUID();
      }
      const result = await api(path, {clips: edits, operation_id: saveOperation.id});
      closeModal();
      toast("已保存版本 " + result.item.version + "，正在制作新版");
      await refresh();
    });
    redraw();
  }
  window.YoutubeRegular = {configure, refine};
})();
