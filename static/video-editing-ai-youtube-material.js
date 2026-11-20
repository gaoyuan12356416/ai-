(() => {
  "use strict";
  const api = "/api/ai-video-editing";
  const targetProducts = { dramawave: "Drama-社媒专用素材", moboreels: "社媒moboreel" };
  const states = {
    draft: "待定稿", queued: "等待入库", copying: "正在传输成片",
    checking: "正在核对素材库回执", unknown: "入库结果待核对", failed: "入库失败", ready: "已入库",
  };
  let active = null;

  async function request(path, body) {
    const response = await fetch(api + path, {
      credentials: "same-origin", cache: "no-store", method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "请求未完成，请刷新状态");
    return result;
  }

  function element(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function field(label, control) {
    const wrap = element("label", label, "field");
    wrap.append(control);
    return wrap;
  }

  async function open(taskId) {
    if (!/^at_[a-f0-9]{32}$/.test(taskId)) return;
    if (active) active.close();
    const dialog = element("dialog");
    active = dialog;
    dialog.setAttribute("aria-label", "定稿入库");
    const title = element("h2", "定稿入库");
    const subtitle = element("p");
    const heading = element("div", undefined, "modal-heading");
    const headingText = element("div");
    headingText.append(title, subtitle);
    const close = element("button", "×", "icon-button");
    close.setAttribute("aria-label", "关闭");
    close.onclick = () => dialog.close();
    heading.append(headingText, close);
    const body = element("div");
    body.style.padding = "18px 24px";
    const message = element("p", "正在读取成片…");
    message.setAttribute("role", "status");
    const controls = element("div");
    const footer = element("div", undefined, "modal-tools");
    body.append(message, controls, footer);
    dialog.append(heading, body);
    document.body.append(dialog);
    dialog.showModal();
    let timer, searchId = 0;
    const closed = () => !dialog.open || active !== dialog;
    dialog.addEventListener("close", () => {
      clearTimeout(timer);
      ++searchId;
      if (active === dialog) active = null;
      dialog.remove();
    }, {once:true});

    const path = "/youtube-material/tasks/" + taskId;
    function showError(error) {
      if (!closed()) message.textContent = error.message || "请求未完成";
    }
    function button(label, action, primary=false) {
      const node = element("button", label, primary ? "primary" : "");
      node.onclick = async () => {
        node.disabled = true;
        try { await action(); } catch (error) { showError(error); }
        finally { if (!closed()) node.disabled = false; }
      };
      return node;
    }
    async function refresh() {
      try {
        const result = await request(path);
        if (!closed()) draw(result.item);
        return result.item;
      } catch (error) {
        if (closed()) return;
        showError(error);
        footer.replaceChildren(button("刷新状态", refresh));
      }
    }
    function draw(item) {
      if (closed()) return;
      clearTimeout(timer);
      ++searchId;
      const association = item.drama;
      subtitle.textContent = `作品版本 ${item.version} · ${item.width} × ${item.height} · ${Math.round(item.duration_ms / 1000)} 秒`;
      const productLabel = association ? targetProducts[association.product] : "";
      message.textContent = [states[item.state], item.error,
        association?.title, productLabel, item.material_id ? "素材 ID " + item.material_id : "",
      ].filter(Boolean).join(" · ");
      controls.replaceChildren();
      footer.replaceChildren();
      if (item.state === "draft") {
        pickDrama(association);
      } else if (item.state === "failed") {
        footer.append(button("重新入库", async () => draw((await request(path + "/retry", {})).item), true));
      } else if (item.state === "ready") {
        footer.append(button("复制素材 ID", () => navigator.clipboard.writeText(item.material_id)));
      } else {
        footer.append(button("刷新状态", refresh));
        timer = setTimeout(refresh, item.state === "unknown" ? 6500 : 2200);
      }
    }

    function pickDrama(known) {
      let chosen = known;
      const country = element("input");
      country.maxLength = 2;
      country.placeholder = "默认按剧目语言";
      const market = field("素材市场（两位国家代码，可选）", country);
      const submit = button("定稿入库", async () => {
        if (!chosen) throw new Error("请先选择这版成片所属的原剧");
        try {
          const result = await request(path + "/finalize", {product: chosen.product,
            key: chosen.source_key, country: country.value.trim().toUpperCase()});
          draw(result.item);
        } catch (error) {
          // A missing HTTP response may follow a successful ledger insert.
          const latest = await refresh();
          if (!latest || latest.state === "draft") showError(error);
        }
      }, true);
      if (!known) {
        const product = element("select");
        for (const [id, label] of Object.entries(targetProducts)) product.add(new Option(label, id));
        const query = element("input");
        query.placeholder = "输入原剧名或剧目 ID";
        const results = element("select");
        results.add(new Option("请先查询剧目", ""));
        submit.disabled = true;
        const search = async () => {
          const seq = ++searchId, id = product.value;
          chosen = null;
          submit.disabled = true;
          results.replaceChildren(new Option("正在查询…", ""));
          const params = new URLSearchParams({product: id, q: query.value.trim(), page: "1"});
          const data = await request("/catalog/dramas?" + params);
          if (closed() || seq !== searchId) return;
          results.replaceChildren(new Option(data.items.length ? "请选择剧目和语言" : "未找到匹配的剧目", ""));
          for (const d of data.items) results.add(new Option(`${d.name} · ${d.language} · ID ${d.id}`, d.key));
        };
        const find = button("查找剧目", search);
        query.onkeydown = event => { if (event.key === "Enter") { event.preventDefault(); find.click(); } };
        product.onchange = () => {
          ++searchId;
          results.replaceChildren(new Option("请先查询剧目", ""));
          chosen = null;
          country.value = "";
          market.hidden = product.value !== "moboreels";
          submit.disabled = true;
        };
        results.onchange = () => {
          chosen = results.value ? { product: product.value, source_key: results.value } : null;
          submit.disabled = !chosen;
        };
        controls.append(field("素材产品", product), field("关联原剧", query), find, field("剧目和语言", results));
        market.hidden = product.value !== "moboreels";
      } else {
        market.hidden = known.product !== "moboreels";
      }
      controls.append(market);
      footer.append(submit);
    }
    await refresh();
  }
  window.YoutubeMaterial = {open};
})();
