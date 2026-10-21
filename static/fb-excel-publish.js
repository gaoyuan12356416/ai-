(() => {
  "use strict";
  const MAX_FILE_BYTES = 2 * 1024 * 1024;
  const modeLabel = value => value === "random_overlay" ? "随机模板" : value === "mixed" ? "逐行加工" : "原片";
  const tokenLabel = value => value === "operator" ? "当前操作人 Token" : "默认 Token 自动轮换";

  async function open(context) {
    const {auth, api, post, dialog, esc, atBJ, errorAt, toast, statuses} = context;
    const existing = document.getElementById("excelPublish");
    if (existing) { existing.focus(); return; }
    const key = `fb-excel-pending:${auth.user?.tenant_key || ""}:${auth.user?.user_id || ""}`;
    let pending = null, storageError = "", preview = null, result = null, working = false;
    try {
      pending = JSON.parse(sessionStorage.getItem(key) || "null");
      if (pending && (!pending.request?.operation_id || !pending.preview_token || typeof pending.allow_repeat !== "boolean")) {
        storageError = "待核对操作记录不完整，请保留当前页面并联系管理员核对发布记录。";
      }
    } catch (_) { storageError = "无法读取待核对操作，请保留当前页面，恢复浏览器会话存储后再试。"; }
    const modal = dialog("excelPublish", "Excel 批量发布",
      `<div class="notice">每行发布一个素材到一个 Page，仅支持 Dramawave。未配置的 Page 会跳过，请先配置后重新提交对应行。发布时间均为北京时间。</div>
       <div id="excelError" class="error" role="alert" hidden></div>
       <div id="excelPendingPanel" class="notice warning" hidden><strong>发布结果待核对</strong><p id="excelPendingText">保留原操作查询，避免重复发布。</p><div class="actions"><button id="excelQueryOperation" class="button">查询原操作</button><button id="excelRetryOperation" class="button">按原操作重试</button></div></div>
       <fieldset id="excelUploadFields"><div class="row spread excel-upload"><label class="field"><span>上传 Excel</span><input id="excelFile" type="file" accept=".xlsx" aria-label="上传 Excel 批量发布文件"></label><a class="button" href="/fb-excel-template.xlsx" download="批量发布模板.xlsx">下载模板</a><button id="excelPreview" class="button primary">上传并校验</button></div><p class="helper">仅支持 .xlsx，最多 200 行、2 MiB。Page ID 和素材 ID 请使用文本格式。加工方式留空为原片，默认 Token 留空为是。</p></fieldset>
       <div id="excelPreviewPanel" hidden><div id="excelSummary" class="summary" role="status"></div><div id="excelSkipped"></div><div id="excelRepeatPanel" class="notice warning" hidden><strong>部分素材已有历史成功发布记录</strong><label class="check"><input id="excelAllowRepeat" type="checkbox">我已核对，允许这些素材再次发布</label><p class="helper">不勾选将跳过历史成功行，其他有效行继续提交。</p></div><div id="excelPreviewRows"></div></div>
       <div id="excelResult" hidden></div>`,
      `<button class="button" data-close>关闭</button><button id="excelNextBatch" class="button" hidden>导入下一批</button><button id="excelSubmit" class="button primary" hidden>确认发布有效行</button>`);
    const $ = id => modal.querySelector("#" + id);
    function lock() {
      if (!modal.isConnected) return;
      $("excelUploadFields").disabled = working || !!pending || !!storageError || !!result;
      modal.querySelectorAll("[data-close]").forEach(button => button.disabled = working);
      $("excelPendingPanel").hidden = !pending;
      $("excelQueryOperation").disabled = working || !!storageError;
      $("excelRetryOperation").disabled = working || !!storageError;
      $("excelAllowRepeat").disabled = working || !!pending;
      $("excelSubmit").hidden = !preview || !!result || !!pending;
      const eligible = (preview?.items || []).filter(item => !item.repeat_required || $("excelAllowRepeat").checked).length;
      $("excelSubmit").disabled = working || !!storageError || !eligible;
      $("excelSubmit").textContent = working ? "正在处理…" : `确认发布 ${eligible} 条有效行`;
      $("excelNextBatch").hidden = !result;
      $("excelNextBatch").disabled = working;
      $("excelPreview").textContent = working && !pending ? "正在校验…" : "上传并校验";
    }
    function savePending(value) {
      if (value) {
        const encoded = JSON.stringify(value);
        sessionStorage.setItem(key, encoded);
        if (sessionStorage.getItem(key) !== encoded) throw new Error("无法保存原操作，尚未提交，请检查浏览器会话存储。");
      } else {
        sessionStorage.removeItem(key);
      }
      pending = value;
    }
    function skippedHtml(rows) {
      return rows?.length ? `<div class="notice warning"><strong>跳过 ${rows.length} 行</strong><div class="table-wrap"><table><thead><tr><th>Excel 行号</th><th>Page / 素材 ID</th><th>原因</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.row_number)}</td><td class="mono">${esc(row.page_id || "—")} / ${esc(row.material_id || "—")}</td><td>${esc(row.message || row.reason || row.code)}</td></tr>`).join("")}</tbody></table></div></div>` : "";
    }
    function renderPreview() {
      $("excelPreviewPanel").hidden = false;
      const summary = preview.summary || {}, rows = preview.items || [], skipped = preview.skipped || [];
      $("excelSummary").innerHTML = `<span>总行数 <strong>${Number(summary.total ?? rows.length + skipped.length)}</strong></span><span>有效 <strong>${rows.length}</strong></span><span>跳过 <strong>${skipped.length}</strong></span>`;
      $("excelSkipped").innerHTML = skippedHtml(skipped);
      $("excelRepeatPanel").hidden = !rows.some(row => row.repeat_required);
      $("excelAllowRepeat").checked = false;
      $("excelPreviewRows").innerHTML = rows.map(row => `<article class="task-card"><h3>第 ${esc(row.row_number)} 行 · ${esc(row.material_name || row.material_id)} → ${esc(row.page_name || row.page_id)}</h3><div class="detail-grid"><span>Page ${esc(row.page_id)}</span><span>素材 ${esc(row.material_id)}</span><span>${esc(modeLabel(row.processing_mode))}</span><span>${esc(tokenLabel(row.credential_mode))}</span><span>${row.schedule_kind === "immediate" ? "立即发布（提交后按顺序开始）" : "预约 " + esc(atBJ(row.planned_at_utc)) + "（北京）"}</span><span>标题：${esc(row.title || "未填写")}</span></div><div class="task-caption">${esc(row.caption)}</div>${row.repeat_required ? '<span class="badge bad">历史已成功发布，默认跳过</span>' : ""}</article>`).join("") || '<p class="empty">没有可提交的有效行，请按上方原因修改后重新上传。</p>';
      lock();
    }
    function renderResult() {
      if (!modal.isConnected || !result) return;
      const batch = result.batch, tasks = batch.items || [], skipped = batch.excel?.skipped || result.skipped || [];
      $("excelPreviewPanel").hidden = true;
      $("excelResult").hidden = false;
      $("excelResult").innerHTML = `<div class="row spread"><div><h3>已创建 ${tasks.length} 条发布任务</h3><p class="mono">批次 ${esc(batch.batch_id || result.batch_id)}</p></div><button id="excelRefreshBatch" class="button">刷新发布结果</button></div><p class="helper">已入队不代表发布成功，以下显示各条任务的当前状态。</p>${skippedHtml(skipped)}${tasks.map(task => `<article class="task-card"><div class="row spread"><h3>第 ${esc(task.row_number ?? task.excel?.row_number ?? "—")} 行 · ${esc(task.material_name || task.material?.material_name || task.material_id)} → ${esc(task.page_name || task.page?.page_name || task.page_id)}</h3><span class="badge ${task.status === "published" ? "good" : ["unknown", "failed"].includes(task.status) ? "bad" : ""}">${esc(statuses[task.status] || task.status)}</span></div><div class="detail-grid"><span>${esc(modeLabel(task.processing_mode))}</span><span>${esc(tokenLabel(task.credential_mode))}</span><span>计划时间 ${esc(atBJ(task.planned_at_utc))}（北京）</span><span>标题：${esc(task.title || "未填写")}</span></div><div class="task-caption">${esc(task.caption)}</div>${task.error_message ? `<p class="error">${esc(task.error_message)}</p>` : ""}<p class="helper mono">任务 ${esc(task.task_id)}</p></article>`).join("")}`;
      $("excelRefreshBatch").onclick = () => run(async () => {
        result = await api("/excel/batches/" + encodeURIComponent(batch.batch_id || result.batch_id));
        renderResult();
      });
      lock();
    }
    function accepted(data) {
      if (!data?.batch || !(data.batch_id || data.batch.batch_id) || !Array.isArray(data.batch.items)) throw new Error("服务未返回完整批次，需查询原操作确认结果。");
      result = data;
      savePending(null);
      errorAt("excelError", "");
      renderResult();
      toast("有效行已加入发布队列");
    }
    async function queryOperation() {
      if (!pending) return false;
      try {
        const data = await api("/excel/operations/" + encodeURIComponent(pending.request.operation_id));
        accepted(data); return true;
      } catch (error) {
        if (modal.isConnected) $("excelPendingText").textContent = error.status === 404 ? "尚未查询到原操作，请保留此记录并按原操作重试；不要重新导入。" : "暂时无法确认原操作：" + error.message;
        return false;
      }
    }
    async function submit(body, recovering = false) {
      // Persist the exact body before any potentially side-effectful request.
      if (!recovering) savePending(structuredClone(body));
      lock();
      try { accepted(await post("/excel/batches", pending)); }
      catch (error) {
        if (error.data?.accepted === false && !recovering) {
          savePending(null);
          errorAt("excelError", new Error(error.message + "；文件和预览已保留，请重新校验后提交。"));
          preview = null;
        } else {
          errorAt("excelError", new Error("发布结果需要核对，已保留原操作。" + error.message));
          await queryOperation();
        }
      }
    }
    async function run(fn) {
      if (working || storageError) return;
      working = true; lock(); errorAt("excelError", "");
      try { await fn(); }
      catch (error) { if (modal.isConnected) errorAt("excelError", error); }
      finally { working = false; lock(); }
    }
    modal.addEventListener("cancel", event => { if (working) event.preventDefault(); });
    $("excelFile").onchange = () => { preview = null; $("excelPreviewPanel").hidden = true; errorAt("excelError", ""); lock(); };
    $("excelAllowRepeat").onchange = lock;
    $("excelPreview").onclick = () => run(async () => {
      if (pending) return;
      const file = $("excelFile").files[0];
      if (!file || !/\.xlsx$/i.test(file.name)) throw new Error("请选择 .xlsx 格式的 Excel 文件。");
      if (!file.size || file.size > MAX_FILE_BYTES) throw new Error("Excel 文件不能为空，且不能超过 2 MiB。");
      preview = null; $("excelPreviewPanel").hidden = true;
      const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("无法读取 Excel 文件，请重新选择。"));
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.readAsDataURL(file);
      });
      const data = await post("/excel/preview", {operation_id: crypto.randomUUID(), filename: file.name, content_base64: content});
      if (!modal.isConnected) return;
      if (!Array.isArray(data.items) || (data.items.length && (!data.request?.operation_id || !data.preview_token || !data.expires_at_utc))) throw new Error("校验结果不完整，请重新校验。");
      preview = data; renderPreview();
    });
    $("excelSubmit").onclick = () => run(async () => {
      if (!preview || pending) return;
      if (Date.parse(preview.expires_at_utc) <= Date.now()) throw new Error("校验结果已过期，请重新点击上传并校验。");
      await submit({request: structuredClone(preview.request), preview_token: preview.preview_token, allow_repeat: $("excelAllowRepeat").checked});
    });
    $("excelQueryOperation").onclick = () => run(queryOperation);
    $("excelRetryOperation").onclick = () => run(async () => {
      if (!pending) return;
      const body = pending;
      try { accepted(await api("/excel/operations/" + encodeURIComponent(body.request.operation_id))); }
      catch (error) {
        if (error.status === 404) await submit(body, true);
        else throw error;
      }
    });
    $("excelNextBatch").onclick = () => {
      if (working || pending) return;
      preview = null; result = null; $("excelFile").value = "";
      $("excelResult").hidden = true; $("excelPreviewPanel").hidden = true;
      errorAt("excelError", ""); lock();
    };
    lock();
    if (storageError) errorAt("excelError", new Error(storageError));
    else if (pending) await run(queryOperation);
  }

  window.FbExcelPublish = Object.freeze({open});
})();
