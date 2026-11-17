(function () {
  "use strict";
  window.normalizeDramaLaunchWindow = function (value) {
    const text = String(value).trim().replace(/～/g, "~").replace(/^(?:\[|【)(.*)(?:\]|】)$/, "$1").trim();
    const range = text.match(/^(-?\d+)\s*~\s*(-?\d+)$/);
    if (range) {
      const low = Number(range[1]), high = Number(range[2]);
      if (low >= -3650 && high <= 3650 && low <= high) return `${low}~${high}`;
    } else if (/^\d+$/.test(text) && Number(text) <= 3650) {
      return Number(text);
    }
    throw new Error("上线窗口请填 0、正整数天数或下限~上限（如 -1~0）；范围为 -3650 至 3650，且下限不能大于上限。");
  };
})();
