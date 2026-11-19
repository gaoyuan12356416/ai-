(() => {
  "use strict";

  function field(value) {
    const group = document.createElement("label");
    group.className = "field";
    const caption = document.createElement("span");
    caption.textContent = "本次选材画像";
    const select = document.createElement("select");
    select.add(new Option("收入画像", "revenue"));
    select.add(new Option("成长画像", "growth"));
    select.value = value === "growth" ? "growth" : "revenue";
    group.append(caption, select);
    return { group, select };
  }

  function restore(picker, request) {
    if (!request.profile_model) picker.select.add(new Option("沿用上次提交", "legacy"));
    picker.select.value = request.profile_model || "legacy";
    picker.select.disabled = true;
  }

  window.XPublishProfileModel = { field, restore };
})();
