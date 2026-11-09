(() => {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const money = cents => new Intl.NumberFormat("en-US", {style:"currency", currency:"USD"}).format(cents / 100);
  const share = bp => bp == null ? "暂无占比" : `${(bp / 100).toFixed(2)}%`;
  const clock = value => new Date(value).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false});
  let items = {}, meta = null, error = "", loading = false, sequence = 0, currentId = "";
  let dialog;
  async function api(path) {
    const response = await fetch(path, {credentials:"same-origin", cache:"no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "标签收入暂不可用");
    return data;
  }
  function cell(id) {
    const item = items[String(id)];
    if (!item) return error ? `<span class="xtr-note">${esc(error)}</span><button type="button" class="button small" data-xtr-retry>重试</button>` : '<span class="xtr-note">正在读取画像…</span>';
    if (item.status === "no_history") return '<span class="xtr-note">该快照暂无已确认发布</span>';
    return `<button type="button" class="xtr-open" data-xtr-open="${esc(id)}" aria-label="查看频道标签收入详情">
      <strong>${esc(money(item.revenue_cents))}</strong>
      ${item.tags.map(tag => `<span class="xtr-tag"><span>${esc(tag.name)}</span><b>${share(tag.share_bp)}</b></span>`).join("")}
      <span class="xtr-note">${item.status === "partial" ? "部分归因待核实 · " : ""}${item.tag_count > 3 ? `共 ${item.tag_count} 个标签 · ` : ""}查看详情 →</span>
    </button>`;
  }
  function warning(info) {
    const notes = [];
    if (info.stale) notes.push("收入快照更新已延迟，正在展示上次成功结果");
    if (info.refresh_failed) notes.push("最近一次标签画像更新失败，保留上次成功结果");
    if (info.missing_dates?.length) notes.push(`区间内 ${info.missing_dates.length} 天源数据未覆盖，不按零统计`);
    return notes.length ? `<div class="xtr-warning" role="status">${esc(notes.join("；"))}</div>` : "";
  }
  function paint() {
    document.querySelectorAll("[data-x-tag-id]").forEach(node => { node.innerHTML = cell(node.dataset.xTagId); });
    let note = document.getElementById("xtrSummary");
    if (!note) {
      const table = document.getElementById("accountRows")?.closest(".table-wrap");
      if (!table) return;
      note = document.createElement("div"); note.id = "xtrSummary"; note.className = "xtr-summary";
      table.before(note);
    }
    note.innerHTML = meta ? `<span>标签收入：${esc(meta.start)} 至 ${esc(meta.end)}（UTC 完整日），USD；同义标签合并后平均分摊。</span>
      <span class="xtr-note">收入快照 ${esc(clock(meta.source_generated_at))}（北京时间）；每小时更新。历史范围以已收录数据为准。</span>${warning(meta)}` : esc(error || "正在读取历史标签收入…");
  }
  async function refresh() {
    if (loading) return;
    loading = true; error = "";
    try { const data = await api("/api/x-channel-tag-revenue"); items = data.items; meta = data.meta; }
    catch (reason) { items = {}; meta = null; error = reason.message; }
    finally { loading = false; paint(); }
  }
  function ensureDialog() {
    if (dialog) return;
    dialog = document.createElement("dialog"); dialog.className = "xtr-dialog";
    dialog.setAttribute("aria-labelledby", "xtrTitle");
    dialog.innerHTML = '<div class="xtr-head"><h2 id="xtrTitle">标签收入详情</h2><button type="button" class="button small" data-xtr-close aria-label="关闭标签收入详情">关闭</button></div><div id="xtrBody" aria-live="polite"></div>';
    document.body.append(dialog);
    dialog.addEventListener("click", event => { if (event.target === dialog || event.target.closest("[data-xtr-close]")) dialog.close(); });
    dialog.addEventListener("close", () => { sequence++; });
  }
  function intro(data) {
    return `<p class="xtr-note">${esc(data.meta.start)} 至 ${esc(data.meta.end)} · UTC · USD · 收入快照 ${esc(clock(data.meta.source_generated_at))}（北京时间）<br>多语言同义标签使用统一中文名称；同剧先合并同义标签，再平均分摊。待映射和歧义标签单独展示，不参与发布偏好匹配。</p>${warning(data.meta)}`;
  }
  async function open(id, tag) {
    ensureDialog(); currentId = id;
    if (!dialog.open) dialog.showModal();
    const request = ++sequence;
    const body = document.getElementById("xtrBody");
    body.innerHTML = '<p>正在读取详情…</p>';
    try {
      const data = await api(`/api/x-channel-tag-revenue/${encodeURIComponent(id)}${tag == null ? "" : `?tag=${encodeURIComponent(tag)}`}`);
      if (request !== sequence) return;
      document.getElementById("xtrTitle").textContent = `${data.account_name}${data.username ? ` (@${data.username})` : ""} · 标签收入`;
      const p = data.portrait;
      if (tag != null) {
        body.innerHTML = `${intro(data)}<button type="button" class="button small" data-xtr-back>← 全部标签</button>
          <p><strong>${esc(p.name)}</strong> · 分摊收入 ${esc(money(p.revenue_cents))} · 占频道 ${share(p.share_bp)}</p>
          <div class="xtr-table-scroll"><table class="xtr-table"><thead><tr><th>剧目</th><th>剧目总收入</th><th>标签数</th><th>本标签分摊收入</th><th>成功发布数</th></tr></thead><tbody>
          ${p.dramas.map(d => `<tr><td><strong>${esc(d.name)}</strong><div class="xtr-note">${esc(d.content_id)} · ${esc(d.language)}</div><div class="xtr-note">${d.labels.length ? esc(d.labels.join("、")) : esc({missing_drama:"未找到匹配剧目",missing_labels:"剧目缺少标签",ambiguous_drama:"剧目映射存在歧义"}[d.mapping_status] || "标签待核实")}</div></td><td>${esc(money(d.revenue_cents))}</td><td>${d.label_count || "—"}</td><td>${esc(money(d.allocated_cents))}</td><td>${d.published_count}</td></tr>`).join("")}
          </tbody></table></div><p class="xtr-note">剧目总收入包含全部标签；本标签分摊收入按去重后的标签数平均计算，显示金额做分币尾差校正。</p>`;
        return;
      }
      body.innerHTML = `${intro(data)}
        <div class="xtr-stats"><span>累计收入 <strong>${esc(money(p.revenue_cents))}</strong></span><span>标签识别覆盖 ${share(p.known_share_bp)}</span><span>${p.drama_count} 部剧 · ${p.published_count} 次成功发布</span></div>
        ${p.status === "partial" ? '<p class="xtr-warning">部分归因存在冲突，以下仅显示已确认归属的收入，缺失部分不按零统计。</p>' : ""}
        <p class="xtr-note">按当前剧目完整标签回算历史收入；去重后平均分摊，不使用链接中的单个标签。未识别标签收入单列。标签资料更新于 ${esc(clock(data.meta.label_updated_at))}（北京时间）。</p>
        ${p.tags.length ? `<div class="xtr-table-scroll"><table class="xtr-table"><thead><tr><th>剧标签（点击查看剧目）</th><th>分摊收入</th><th>收入占比</th><th>剧目数</th><th>成功发布数</th></tr></thead><tbody>
          ${p.tags.map(row => `<tr><td><button type="button" class="xtr-tag-link" data-xtr-tag="${esc(row.key)}">${esc(row.name)}</button></td><td>${esc(money(row.revenue_cents))}</td><td>${share(row.share_bp)}</td><td>${row.drama_count}</td><td>${row.published_count}</td></tr>`).join("")}
        </tbody><tfoot><tr><th>合计</th><td>${esc(money(p.revenue_cents))}</td><td>${p.tags.every(t => t.share_bp != null) ? "100.00%" : "—"}</td><td colspan="2">${p.tags.length} 个标签</td></tr></tfoot></table></div>` : '<p>该快照暂无已确认发布记录。</p>'}
        <p class="xtr-note">一部剧可包含多个标签，剧目数和发布数不可跨标签相加。总收入为零或存在负值时不展示占比；收入金额沿用社媒效果报表口径。</p>`;
    } catch (reason) {
      if (request === sequence) body.innerHTML = `<p>${esc(reason.message)}</p><button type="button" class="button small" data-xtr-back>重新读取频道详情</button>`;
    }
  }
  document.addEventListener("click", event => {
    const target = event.target.closest("[data-xtr-open],[data-xtr-retry],[data-xtr-tag],[data-xtr-back]");
    if (!target) return;
    if (target.hasAttribute("data-xtr-open")) open(target.dataset.xtrOpen);
    else if (target.hasAttribute("data-xtr-retry")) refresh();
    else if (target.hasAttribute("data-xtr-tag")) open(currentId, target.dataset.xtrTag);
    else open(currentId);
  });
  window.XTagRevenue = {cell, refresh};
  if (document.querySelector("[data-x-tag-id]")) refresh();
})();
