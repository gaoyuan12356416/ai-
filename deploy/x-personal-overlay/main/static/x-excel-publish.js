(() => {
  "use strict";
  const P=window.XVideoPublish;
  const HEADERS = ["账号 ID", "素材 ID", "发布文案", "发布时间（北京时间）", "加工方式"];
  const MAX_BYTES = 2 * 1024 * 1024;
  const MAX_ROWS = 200;
  const MAX_EXPANDED = 16 * 1024 * 1024;
  const MAX_CELLS = 200000;
  const errorText = error => error?.message || String(error);
  const hasValue = cell => !!cell && (typeof cell.f === "string" || (cell.v !== undefined && cell.v !== null && (typeof cell.v !== "string" || !!cell.v.trim())));
  const cellKeys = sheet => Object.keys(sheet || {}).filter(key => /^[A-Z]+[1-9][0-9]*$/.test(key));
  const displayId = cell => cell?.v === undefined || cell?.v === null ? "—" : String(cell.v).slice(0, 128);

  // Check the ZIP directory before SheetJS expands a user-selected workbook.
  function checkArchive(buffer) {
    const bytes = new Uint8Array(buffer), view = new DataView(buffer);
    if (bytes.length < 22 || view.getUint32(0, true) !== 0x04034b50) throw new Error("无法读取 .xlsx 文件，请使用下载模板重新保存。");
    let end = -1;
    for (let offset = bytes.length - 22; offset >= Math.max(0, bytes.length - 65557); offset--) {
      if (view.getUint32(offset, true) === 0x06054b50 && offset + 22 + view.getUint16(offset + 20, true) === bytes.length) { end = offset; break; }
    }
    if (end < 0) throw new Error("Excel 压缩结构无效。");
    const count = view.getUint16(end + 10, true), size = view.getUint32(end + 12, true), start = view.getUint32(end + 16, true);
    if (view.getUint16(end + 4, true) || view.getUint16(end + 6, true) || view.getUint16(end + 8, true) !== count || !count || count > 256 || start + size > end) throw new Error("不支持此 Excel 压缩结构，请使用下载模板。");
    let offset = start, expanded = 0;
    const names = new Set(), decoder = new TextDecoder();
    for (let i = 0; i < count; i++) {
      if (offset + 46 > end || view.getUint32(offset, true) !== 0x02014b50) throw new Error("Excel 压缩目录损坏。");
      const flags = view.getUint16(offset + 8, true), method = view.getUint16(offset + 10, true);
      const compressed = view.getUint32(offset + 20, true), uncompressed = view.getUint32(offset + 24, true);
      const nameLength = view.getUint16(offset + 28, true), extraLength = view.getUint16(offset + 30, true), commentLength = view.getUint16(offset + 32, true);
      const localOffset = view.getUint32(offset + 42, true), next = offset + 46 + nameLength + extraLength + commentLength;
      if (next > start + size || flags & 1 || ![0, 8].includes(method) || compressed === 0xffffffff || uncompressed === 0xffffffff || localOffset + 30 > start) throw new Error("不支持加密或异常压缩的 Excel 文件。");
      const name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
      if (names.has(name) || /(?:^|\/)\.\.(?:\/|$)|vbaproject\.bin|macrosheets\//i.test(name)) throw new Error("Excel 包含重复文件或不支持的宏内容。");
      if (view.getUint32(localOffset, true) !== 0x04034b50) throw new Error("Excel 文件内容损坏。");
      const dataStart = localOffset + 30 + view.getUint16(localOffset + 26, true) + view.getUint16(localOffset + 28, true);
      if (dataStart + compressed > start) throw new Error("Excel 压缩数据越界。");
      names.add(name); expanded += uncompressed;
      if (expanded > MAX_EXPANDED) throw new Error("Excel 解压内容超过 16 MiB，请使用下载模板并删除多余内容。");
      offset = next;
    }
    if (offset !== start + size || !names.has("xl/workbook.xml")) throw new Error("请选择标准 .xlsx 工作簿。");
  }

  function idValue(cell, label) {
    const value = cell?.v;
    if (cell?.t === "e" || cell?.t === "b" || typeof value === "boolean") throw new Error(`${label}必须为完整文本 ID。`);
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value) || value <= 0 || String(value).length > 15) throw new Error(`${label}可能已被 Excel 舍入，请设为文本并重新填写完整 ID。`);
      return String(value);
    }
    if (typeof value !== "string" || !value.trim() || value.trim().length > 128 || /[\u0000-\u001f]/.test(value)) throw new Error(`${label}不能为空，且必须为完整文本 ID。`);
    return value.trim();
  }

  function textValue(cell, label) {
    if (!hasValue(cell)) return "";
    if (cell.t === "e" || typeof cell.v !== "string") throw new Error(`${label}请填写文本。`);
    if (cell.v.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(cell.v)) throw new Error(`${label}包含无效字符或内容过长。`);
    return cell.v.trim();
  }

  function timeValue(cell, date1904) {
    if (!hasValue(cell)) return "";
    let raw;
    if (typeof cell.v === "number") {
      if (!cell.z || !window.XLSX.SSF.is_date(cell.z)) throw new Error("发布时间请设置为 Excel 日期时间，或填写 YYYY-MM-DD HH:mm:ss 北京时间文本。");
      const parts = window.XLSX.SSF.parse_date_code(cell.v, { date1904 });
      if (!parts || parts.y < 1900) throw new Error("发布时间不是有效日期时间。");
      raw = `${parts.y}-${String(parts.m).padStart(2, "0")}-${String(parts.d).padStart(2, "0")}T${String(parts.H).padStart(2, "0")}:${String(parts.M).padStart(2, "0")}:${String(parts.S).padStart(2, "0")}`;
    } else if (typeof cell.v === "string") raw = cell.v.trim().replace(" ", "T");
    else throw new Error("发布时间请填写北京日期及时间，留空为立即发布。");
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(raw)) throw new Error("发布时间格式应为 YYYY-MM-DD HH:mm:ss（北京时间）。");
    const stamp = P.parseTime(raw);
    if (!Number.isFinite(stamp)) throw new Error("发布时间不是有效的北京日期时间。");
    if (stamp <= Date.now()) throw new Error("发布时间已过，请填写未来北京时间或留空立即发布。");
    return raw;
  }

  function parseWorkbook(buffer) {
    if (!buffer.byteLength || buffer.byteLength > MAX_BYTES) throw new Error("文件不能为空，且不能超过 2 MiB。");
    checkArchive(buffer);
    const workbook = window.XLSX.read(buffer, { type: "array", cellFormula: true, cellDates: false, cellNF: true, dense: false });
    if (!workbook.SheetNames.length) throw new Error("Excel 没有工作表。");
    let scanned = 0;
    for (let index = 0; index < workbook.SheetNames.length; index++) {
      const sheet = workbook.Sheets[workbook.SheetNames[index]], keys = cellKeys(sheet);
      scanned += keys.length;
      if (scanned > MAX_CELLS) throw new Error("工作簿单元格过多，请使用下载模板重新保存。");
      if (index > 0 && keys.some(key => hasValue(sheet[key]))) throw new Error("仅支持第一张工作表；其他工作表存在数据，请移除或另存为单表文件。");
    }
    const sheet = workbook.Sheets[workbook.SheetNames[0]], rows = new Map();
    for (const key of cellKeys(sheet)) {
      const cell = sheet[key];
      if (!hasValue(cell)) continue;
      const position = window.XLSX.utils.decode_cell(key), number = position.r + 1;
      if (number > 10001) throw new Error("工作表行号过大，请删除多余空行后重试。");
      if (!rows.has(number)) rows.set(number, new Map());
      rows.get(number).set(position.c, cell);
    }
    const header = rows.get(1) || new Map();
    if (HEADERS.some((name, index) => header.get(index)?.f !== undefined || String(header.get(index)?.v ?? "").trim() !== name) || [...header.keys()].some(column => column >= HEADERS.length)) throw new Error("表头必须与下载模板的五列完全一致，且顺序相同。");
    rows.delete(1);
    if (!rows.size) throw new Error("文件只有表头或没有数据行，请填写后重新校验。");
    if (rows.size > MAX_ROWS) throw new Error("单次最多导入 200 个非空数据行，请拆分文件。");
    const valid = [], skipped = [], seen = new Set();
    const date1904 = !!workbook.Workbook?.WBProps?.date1904;
    for (const [number, cells] of [...rows.entries()].sort((a, b) => a[0] - b[0])) {
      let accountId = displayId(cells.get(0)), materialId = displayId(cells.get(1));
      try {
        if ([...cells.values()].some(cell => typeof cell.f === "string")) throw new Error("不支持公式，请粘贴为实际值后重试。");
        if ([...cells.keys()].some(column => column >= HEADERS.length)) throw new Error("模板只支持五列，请移除多余数据。");
        accountId = idValue(cells.get(0), "账号 ID"); materialId = idValue(cells.get(1), "素材 ID");
        if (!/^[1-9][0-9]{0,30}$/.test(accountId) || !/^[1-9][0-9]{0,30}$/.test(materialId)) throw new Error("账号和素材 ID 必须是完整数字文本。");
        const template = textValue(cells.get(2), "发布文案");
        if (template && P.templateError(template)) throw new Error(P.templateError(template));
        const time = timeValue(cells.get(3), date1904);
        const mode = textValue(cells.get(4), "加工方式") || "原片";
        if (!["原片", "随机加工"].includes(mode)) throw new Error("加工方式仅支持原片或随机加工，留空为原片。");
        valid.push({row_number:number,x_user_id:accountId,material_id:materialId,caption_template:template,scheduled_at_utc:time?new Date(P.parseTime(time)).toISOString():null,processing_mode:mode === "随机加工" ? "random_overlay" : "original"});
      } catch (error) { skipped.push({row_number:number,x_user_id:accountId,material_id:materialId,message:errorText(error)}); }
    }
    return { total: rows.size, valid, skipped };
  }

  P.parseWorkbook = parseWorkbook;
  P.openExcel = async () => {
    if (!P.allowed(P.auth)) throw new Error("当前账号没有 X 个人视频发布权限。");
    let preview=null,operation="",working=false;
    const modal=P.dialog("xpExcel","Excel 批量发布",`<div class="xp-notice">.xlsx 文件在浏览器本地解析，不上传原文件。只提交有效数据行供服务端核对。每行一个视频与账号配对，账号 ID 填写完整的 X 平台 ID；空文案使用账号默认文案。</div><div id="xpExcelError" class="xp-error" hidden></div><div class="xp-actions"><a class="xp-button" href="/templates/x-publishing-template.xlsx" download>下载空模板</a><button class="xp-button" id="xpExcelExample">下载格式示例</button></div><label class="xp-field">选择文件（最多 200 条数据，2 MiB）<input id="xpExcelFile" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></label><p class="xp-muted">固定五列：账号 ID / 素材 ID / 发布文案 / 发布时间（北京时间） / 加工方式。相同账号和素材保留首条有效行；进行中和结果待核对的重复配对不可提交。</p><div class="xp-actions"><button id="xpExcelValidate" class="xp-button primary">本地校验并核对</button></div><div id="xpExcelPreview"></div>`,`<button class="xp-button" data-xp-close>关闭</button><button id="xpExcelConfirm" class="xp-button primary" disabled>核对有效行</button>`);
    const lock=()=>{if(!modal.isConnected)return;P.$("xpExcelFile").disabled=working;P.$("xpExcelValidate").disabled=working;P.$("xpExcelConfirm").disabled=working||!preview?.items?.length;modal.querySelectorAll("[data-xp-close]").forEach(button=>button.disabled=working);};
    modal.addEventListener("cancel",event=>{if(working)event.preventDefault();});
    P.$("xpExcelExample").onclick=()=>{if(!window.XLSX){P.error("xpExcelError","Excel 组件未加载，请刷新重试。");return;}const wb=XLSX.utils.book_new(),sheet=XLSX.utils.aoa_to_sheet([HEADERS,["1900000000000000001","999999991","{name}\n{url}","","原片"],["1900000000000000002","999999991","","","随机加工"]]);sheet["!cols"]=[{wch:28},{wch:22},{wch:45},{wch:28},{wch:16}];XLSX.utils.book_append_sheet(wb,sheet,"格式示例");XLSX.writeFile(wb,"X发布格式示例_请替换虚构ID.xlsx");};
    P.$("xpExcelFile").onchange=()=>{preview=null;P.$("xpExcelPreview").innerHTML="";P.error("xpExcelError","");lock();};
    P.$("xpExcelValidate").onclick=async()=>{if(working)return;working=true;preview=null;lock();P.error("xpExcelError","");try{if(P.pending("batch")){P.showPending(P.accepted || (result=>location.assign("/x-video-publish.html?view=records&batch="+encodeURIComponent(result.batch_id || result.batch?.batch_id))));return;}const file=P.$("xpExcelFile").files[0];if(!file || !/\.xlsx$/i.test(file.name))throw new Error("请选择 .xlsx 文件。");if(file.size>MAX_BYTES || !file.size)throw new Error("文件不能为空且不能超过 2 MiB。");if(!window.XLSX)throw new Error("Excel 解析组件未加载，请刷新重试。");const parsed=parseWorkbook(await file.arrayBuffer());P.$("xpExcelPreview").innerHTML=`<p>本地读取 ${parsed.total} 行，等待服务端核对…</p>${P.skipped(parsed.skipped)}`;if(!parsed.valid.length){P.$("xpExcelPreview").innerHTML=`<p>有效 0 行 · 跳过 ${parsed.skipped.length} 行；没有创建批次。</p>${P.skipped(parsed.skipped)}`;return;}operation=P.uid();preview=await P.post("/excel/preview",{idempotency_key:operation,source:"excel",filename:file.name,rows:parsed.valid,client_skipped:parsed.skipped,total_rows:parsed.total});if(!modal.isConnected)return;if(!Array.isArray(preview.items))throw new Error("服务端预检结果不完整。");const skipped=[...parsed.skipped,...(preview.skipped || [])].sort((a,b)=>a.row_number-b.row_number);preview.skipped=skipped;P.$("xpExcelPreview").innerHTML=`<h3>有效 ${preview.items.length} 行 · 跳过 ${skipped.length} 行</h3>${P.skipped(skipped)}${P.taskRows(preview.items)}${!preview.items.length?'<div class="xp-empty">没有可提交的有效行，未创建批次。</div>':""}`;}catch(error){P.error("xpExcelError",error);}finally{working=false;lock();}};
    P.$("xpExcelConfirm").onclick=()=>{try{if(!preview?.items?.length)return;P.confirm(preview,operation,result=>{modal.close();(P.accepted || (r=>location.assign("/x-video-publish.html?view=records&batch="+encodeURIComponent(r.batch_id || r.batch?.batch_id))))(result);});}catch(error){P.error("xpExcelError",error);}};
    lock();
  };
})();
