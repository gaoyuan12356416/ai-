    // SYNTHESIS_MATERIAL_SOURCE_V1: embedded in index.html by the release patch.
    const materialUploadBusy = new Set();
    function materialUploadText(value) {
      return String(value || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    }
    function materialUploadStatus(item) {
      const labels = {queued:"等待上传", copying:"校验视频", submitting:"提交中", verifying:"入库确认中", unknown_outcome:"入库结果待核实", succeeded:"已上传", failed:"上传失败"};
      return (labels[item.status] || "状态待核实") + (item.material_id ? ` · 素材 ID ${item.material_id}` : "") + (item.error ? `：${item.error}` : "");
    }
    function materialUploadActions(job) {
      if (job.status !== "done") return "";
      const videos = selectableMaterials(job, false);
      if (!videos.length) return "";
      const records = Array.isArray(job.material_source_uploads) ? job.material_source_uploads : [];
      const selected = videos.map(video => ({video, item:records.find(item => item.material_kind === video[0] && item.source_url === video[2])}));
      const complete = selected.every(({item}) => item && item.status === "succeeded");
      const busy = materialUploadBusy.has(job.job_id);
      const status = selected.filter(({item}) => item).map(({video,item}) => `<div>${materialUploadText(video[1])}：${materialUploadText(materialUploadStatus(item))}</div>`).join("");
      const historyError = job.material_source_uploads === null ? '<div>入库状态暂不可用，请刷新核实</div>' : "";
      return `<button class="btn btn-secondary mini" type="button" ${complete || busy ? "disabled" : ""} onclick="event.stopPropagation(); uploadSynthesisMaterial('${job.job_id}')">${complete ? "已上传自定义素材库" : busy ? "正在提交…" : "上传到自定义素材库"}</button>${status || historyError ? `<div class="hint" style="flex-basis:100%;line-height:1.6">${status}${historyError}</div>` : ""}`;
    }
    async function uploadSynthesisMaterial(jobId) {
      if (materialUploadBusy.has(jobId)) return;
      materialUploadBusy.add(jobId);
      try {
        const job = await api(`/api/drama-material/jobs/${jobId}`);
        const material = await chooseMaterial(job, false);
        if (!material) return;
        const result = await api(`/api/drama-material/jobs/${jobId}/material-source`, {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({material_kind:material[0]})
        });
        toast(materialUploadStatus(result.item));
      } catch (error) {
        showError("素材上传未完成，请刷新查看入库状态", error);
      } finally {
        materialUploadBusy.delete(jobId);
        await loadJobs().catch(() => {});
        if (state.selectedJob && state.selectedJob.job_id === jobId && els.detailDrawer.classList.contains("open")) {
          await viewJob(jobId).catch(() => {});
        }
      }
    }

