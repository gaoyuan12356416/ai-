(() => {
  "use strict";
  const known = ["Dramawave", "Moboreels", "Moboshort"];
  const labels = { Dramawave: "DramaWave", Moboreels: "MoboReels", Moboshort: "MoboShort" };
  function render(config = {}) {
    const root = document.getElementById("materialProducts");
    const values = Array.isArray(config.material_products)
      ? config.material_products : [config.material_product || config.product || "Dramawave"];
    const selected = new Set(values.map(value => String(value).trim().toLowerCase()));
    const options = root.dataset.sourceOption === "true" ? [...known, "DramawaveSource"] : [...known];
    values.forEach(value => {
      if (!options.some(option => option.toLowerCase() === String(value).toLowerCase())) options.push(String(value));
    });
    root.replaceChildren();
    options.forEach(value => {
      const label = document.createElement("label");
      label.style.cssText = "display:inline-flex;align-items:center;gap:7px;margin:0 18px 8px 0;cursor:pointer";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "material_product";
      input.value = value;
      input.checked = selected.has(value.toLowerCase());
      input.style.cssText = "width:16px;height:16px;margin:0";
      const text = document.createElement("span");
      text.textContent = labels[value] || value;
      label.append(input, text);
      root.append(label);
    });
  }
  function read() {
    const values = Array.from(document.querySelectorAll('#materialProducts input:checked')).map(input => input.value);
    if (!values.length) throw new Error("请至少选择一个素材产品。");
    return values;
  }
  window.MaterialProductPicker = Object.freeze({ render, read });
})();
