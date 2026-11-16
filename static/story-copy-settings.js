(() => {
  "use strict";
  const script = document.currentScript;
  const endpoint = script.dataset.endpoint;
  const header = document.querySelector("main h1, h1");
  if (!header || document.getElementById("story-copy-option")) return;

  const panel = document.createElement("div");
  panel.id = "story-copy-option";
  panel.style.cssText = "display:flex;align-items:center;gap:.8em;flex-wrap:wrap;margin:.65em 0;font-size:.92rem";
  const label = document.createElement("label");
  label.style.cssText = "display:inline-flex;align-items:center;gap:.45em;cursor:pointer";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.disabled = true;
  checkbox.indeterminate = true;
  label.append(checkbox, "启用 AI 剧情分行文案");
  const status = document.createElement("span");
  status.setAttribute("role", "status");
  status.style.opacity = ".8";
  panel.append(label, status);
  header.insertAdjacentElement("afterend", panel);
  let saved;
  async function update(change) {
    checkbox.disabled = true;
    status.textContent = change ? "正在保存…" : "读取中…";
    try {
      const response = await fetch("/api/story-copy/" + endpoint, change ? {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: checkbox.checked })
      } : { credentials: "same-origin", cache: "no-store" });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || "读取失败");
      saved = value.enabled;
      checkbox.checked = saved;
      checkbox.indeterminate = false;
      checkbox.disabled = !value.editable;
      status.textContent = saved ? "本端新任务启用，异常时使用现行文案" : "本端新任务使用现行文案";
    } catch (error) {
      checkbox.indeterminate = true;
      checkbox.disabled = true;
      status.textContent = "配置状态待核对 · ";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "重新读取";
      retry.addEventListener("click", () => update(false), { once: true });
      status.append(retry);
    }
  }
  checkbox.addEventListener("change", () => update(true));
  update(false);
})();
