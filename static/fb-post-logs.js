    (() => {
      const state = { auth:null, page:1, pageSize:20, total:0, items:[], selected:new Map(), loading:false, busy:false, loadId:0, pending:null };
      const byId = id => document.getElementById(id);
      const text = (node, value) => { node.textContent = value == null || value === "" ? "—" : String(value); };
      const formatTime = value => {
        if (!value) return "—";
        const raw = String(value);
        const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(raw) ? raw : raw.replace(" ","T") + "Z";
        const date = new Date(normalized);
        return Number.isNaN(date.getTime()) ? raw : new Intl.DateTimeFormat("zh-CN", { dateStyle:"short", timeStyle:"medium", hour12:false, timeZone:"Asia/Shanghai" }).format(date);
      };
      const safeLink = (value, post) => {
        try {
          const u = new URL(value);
          if (u.protocol !== "https:" || u.username || u.password || u.port) return "";
          if (post && !(u.hostname === "facebook.com" || u.hostname.endsWith(".facebook.com") || u.hostname === "fb.watch")) return "";
          return u.href;
        } catch (_) { return ""; }
      };
      async function api(path, options = {}) {
        const response = await fetch(path, { credentials:"same-origin", cache:"no-store", ...options });
        const body = await response.text();
        let payload = {};
        try { payload = body ? JSON.parse(body) : {}; } catch (_) { payload = {}; }
        if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
        return payload;
      }
      function appendCell(row, primary, secondary) {
        const cell = document.createElement("td");
        text(cell, primary);
        if (secondary) {
          const sub = document.createElement("div");
          sub.className = "subtle";
          text(sub, secondary);
          cell.appendChild(sub);
        }
        row.appendChild(cell);
        return cell;
      }
      function setEmpty(message) {
        const body = byId("logRows");
        body.replaceChildren();
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.colSpan = 12;
        cell.className = "empty";
        text(cell, message);
        row.appendChild(cell);
        body.appendChild(row);
      }
      function renderRows(items) {
        const body = byId("logRows");
        body.replaceChildren();
        if (!items.length) { setEmpty("没有符合当前筛选条件的发布记录。"); return; }
        items.forEach(item => {
          const row = document.createElement("tr");
          const checkCell = document.createElement("td");
          const check = document.createElement("input");
          check.type = "checkbox";
          check.dataset.queueId = item.queue_id;
          check.setAttribute("aria-label", `选择队列 ${item.queue_id}`);
          check.disabled = state.busy || !item.can_reset;
          check.checked = state.selected.has(item.queue_id);
          check.addEventListener("change", () => {
            if (check.checked) state.selected.set(item.queue_id, item); else state.selected.delete(item.queue_id);
            selectionChanged();
          });
          checkCell.appendChild(check); row.appendChild(checkCell);
          appendCell(row, item.run_date, [item.queue_id, item.batch_label].filter(Boolean).join(" · "));
          appendCell(row, item.source_label, item.template_name);
          appendCell(row, item.page_name, item.page_id);
          appendCell(row, item.material_name, item.material_id);
          appendCell(row, item.drama_name, [item.content_id, item.language].filter(Boolean).join(" · "));
          appendCell(row, item.pool_sequence || "—", item.pool_item_id ? "素材池条目 " + item.pool_item_id : "");
          const statusCell = document.createElement("td");
          const badge = document.createElement("span");
          badge.className = "status " + item.status_class;
          text(badge, item.status_label);
          statusCell.appendChild(badge);
          if (item.attempt_count) {
            const attempts = document.createElement("div");
            attempts.className = "subtle";
            text(attempts, `尝试 ${item.attempt_count} 次`);
            statusCell.appendChild(attempts);
          }
          row.appendChild(statusCell);
          const linksCell = document.createElement("td");
          linksCell.className = "links";
          [[safeLink(item.post_url, true), "打开帖子"], [safeLink(item.short_url, false), "打开短链"]].forEach(([href,label]) => {
            if (!href) return;
            const link = document.createElement("a");
            link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; text(link, label);
            linksCell.appendChild(link);
          });
          if (item.graph_post_id) {
            const id = document.createElement("div"); id.className = "subtle"; text(id, "平台编号 " + item.graph_post_id); linksCell.appendChild(id);
          }
          if (!linksCell.childNodes.length) text(linksCell, "—");
          row.appendChild(linksCell);
          appendCell(row, formatTime(item.published_at || item.finished_at || item.updated_at), `计划：${formatTime(item.planned_at)}`);
          const errorCell = appendCell(row, item.error_code || "—", item.error_message || "");
          if (item.error_code || item.error_message) errorCell.className = "error";
          const actions = document.createElement("td");
          const buttons = document.createElement("div"); buttons.className = "row-actions";
          [["reset","重置发布"],["stop","停止发布"]].forEach(([action,label]) => {
            const button = document.createElement("button"); button.type = "button"; button.className = "button";
            button.textContent = label; button.disabled = state.busy || !item[`can_${action}`];
            button.title = item[`${action}_reason`] || label;
            button.addEventListener("click", () => performAction(action, [item]));
            buttons.appendChild(button);
          });
          actions.appendChild(buttons);
          if (!item.can_reset && item.reset_reason) {
            const reason = document.createElement("div"); reason.className = "subtle"; reason.textContent = item.reset_reason; actions.appendChild(reason);
          }
          row.appendChild(actions);
          body.appendChild(row);
        });
        selectionChanged();
      }
      function queryString() {
        const params = new URLSearchParams({ page:String(state.page), page_size:String(state.pageSize) });
        [["run_date","runDate"],["page_id","pageId"],["material_id","materialId"],["task_source","taskSource"],["status","status"],["unknown_outcome","unknownOutcome"]].forEach(([key,id]) => {
          const value = byId(id).value.trim();
          if (value) params.set(key, value);
        });
        return params.toString();
      }
      async function loadLogs() {
        const requestId = ++state.loadId;
        state.loading = true; state.selected.clear(); selectionChanged();
        setEmpty("正在加载…");
        try {
          const result = await api(`/api/admin/fb-posts/logs?${queryString()}`);
          if (requestId !== state.loadId) return;
          const items = Array.isArray(result.items) ? result.items : [];
          const pagination = result.pagination && typeof result.pagination === "object" ? result.pagination : result;
          state.total = Number(pagination.total || 0);
          state.page = Number(pagination.page || state.page);
          state.pageSize = Number(pagination.page_size || state.pageSize);
          state.items = items; renderRows(items);
          text(byId("resultTotal"), state.total);
          const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
          text(byId("pageInfo"), `第 ${state.page} / ${pages} 页，共 ${state.total} 条`);
          byId("prevPage").disabled = state.page <= 1;
          byId("nextPage").disabled = state.page >= pages;
        } catch (error) {
          if (requestId !== state.loadId) return;
          state.total = 0; text(byId("resultTotal"), 0); text(byId("pageInfo"), "查询失败"); byId("prevPage").disabled = byId("nextPage").disabled = true;
          state.items = []; setEmpty(`加载失败：${error.message}`);
        } finally {
          if (requestId === state.loadId) { state.loading = false; selectionChanged(); }
        }
      }

      function selectionChanged() {
        const available = state.items.filter(item => item.can_reset);
        const count = state.selected.size;
        text(byId("selectionCount"), `已选 ${count} 条（当前页）`);
        byId("bulkReset").disabled = state.loading || state.busy || !count || !!state.pending;
        byId("clearSelection").disabled = state.busy || !count;
        byId("selectPage").disabled = state.loading || state.busy || !available.length;
        byId("selectPage").checked = !!available.length && count === available.length;
        byId("selectPage").indeterminate = count > 0 && count < available.length;
        byId("retryAction").classList.toggle("hidden", !state.pending);
        byId("retryAction").disabled = state.busy;
      }
      function showActionResult(message) {
        const node = byId("actionResult"); node.textContent = message; node.classList.remove("hidden");
      }
      async function performAction(action, items, retry = false) {
        if (state.busy) return;
        if (!retry && state.pending) { showActionResult("上次操作结果尚未确认，请先重试上次操作核对结果。"); return; }
        if (!retry) {
          const label = action === "reset" ? "重置并重新执行发布" : "停止发布";
          if (!window.confirm(`确认对选中的 ${items.length} 条队列${label}？\n${action === "reset" ? "将使用原素材、Page 和链接重新入队；无法安全重置的记录会逐条跳过。" : "仅停止这些尚未执行的队列；正在执行的记录会跳过。"}`)) return;
          state.pending = {operation_id:crypto.randomUUID(),action,items:items.map(item => ({queue_id:item.queue_id,version:item.action_version}))};
          try { sessionStorage.setItem("fb-log-pending-action", JSON.stringify(state.pending)); } catch (_) {}
        }
        state.busy = true; renderRows(state.items); selectionChanged();
        try {
          const result = await api("/api/admin/fb-posts/queue-actions", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(state.pending)});
          showActionResult(`成功 ${result.succeeded} 条，未处理 ${result.skipped} 条\n` + result.items.map(item => `队列 ${item.queue_id}：${item.message}`).join("\n"));
          state.pending = null;
          try { sessionStorage.removeItem("fb-log-pending-action"); } catch (_) {}
          await loadLogs();
        } catch (error) {
          showActionResult(`操作结果尚未确认：${error.message}。请点击“重试上次操作”，系统会核对同一操作，避免重复重置。`);
        } finally { state.busy = false; renderRows(state.items); selectionChanged(); }
      }
      byId("selectPage").addEventListener("change", event => {
        state.selected.clear();
        if (event.target.checked) state.items.filter(item => item.can_reset).forEach(item => state.selected.set(item.queue_id,item));
        renderRows(state.items);
      });
      byId("clearSelection").addEventListener("click", () => { state.selected.clear(); renderRows(state.items); });
      byId("bulkReset").addEventListener("click", () => performAction("reset", Array.from(state.selected.values())));
      byId("retryAction").addEventListener("click", () => performAction(null, null, true));
      async function init() {
        try { state.auth = await api("/api/ui/topbar"); }
        catch (_) { state.auth = { authenticated:false, login_url:"/api/auth/feishu/login" }; }
        await window.QuickNav.render({ container:"#quickNav", auth:state.auth, activeKey:"fbPostLogs" });
        window.UiTopbar.render({ auth:state.auth, userCard:"#userCard", authButton:"#authButton", refreshButton:"#refreshPage" });
        byId("authButton").addEventListener("click", () => window.UiTopbar.handleAuthAction({ auth:state.auth, api }));
        const user = state.auth && state.auth.user;
        if (!(state.auth && state.auth.authenticated && user)) { byId("loginGate").classList.remove("hidden"); return; }
        if (!user.is_admin) { byId("adminGate").classList.remove("hidden"); return; }
        byId("pageRoot").classList.remove("hidden");
        try { state.pending = JSON.parse(sessionStorage.getItem("fb-log-pending-action") || "null"); } catch (_) {}
        if (state.pending) showActionResult("上次操作结果尚未确认，请点击“重试上次操作”核对同一操作的结果。");
        await loadLogs();
      }
      byId("filters").addEventListener("submit", event => { event.preventDefault(); state.page = 1; loadLogs(); });
      byId("resetFilters").addEventListener("click", () => { byId("filters").reset(); state.page = 1; loadLogs(); });
      byId("prevPage").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadLogs(); } });
      byId("nextPage").addEventListener("click", () => { if (state.page * state.pageSize < state.total) { state.page += 1; loadLogs(); } });
      init();
    })();
