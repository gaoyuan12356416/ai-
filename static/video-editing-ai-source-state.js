/* Pure source-intent helpers shared by the UI and regression tests. */
(() => {
  const key = (item) =>
    item.kind === "catalog"
      ? JSON.stringify([item.product, item.key, item.episode])
      : "asset:" + item.asset_id;
  const same = (a, b) =>
    key(a) === key(b) ||
    !!(a.asset_id && b.asset_id && a.asset_id === b.asset_id);
  const intent = (item) =>
    item.kind === "catalog"
      ? {
          kind: "catalog",
          product: item.product,
          key: item.key,
          episode: item.episode,
          name: item.name,
          ...(item.asset_id ? { asset_id: item.asset_id } : {}),
        }
      : { kind: "asset", asset_id: item.asset_id || item.id, name: item.name };
  function unique(items) {
    const result = [];
    for (const item of items)
      if (!result.some((x) => same(x, item))) result.push(intent(item));
    return result;
  }
  function toggle(items, item) {
    const found = items.findIndex((x) => same(x, item));
    if (found >= 0) return items.filter((_, i) => i !== found);
    if (items.length >= 20) throw new Error("一次创作最多选择 20 个不同素材");
    return [...items, intent(item)];
  }
  function move(items, index, direction) {
    const result = [...items],
      target = index + direction;
    if (
      index < 0 ||
      target < 0 ||
      index >= result.length ||
      target >= result.length
    )
      return result;
    [result[index], result[target]] = [result[target], result[index]];
    return result;
  }
  const api = { key, same, intent, unique, toggle, move };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.AIEditorSourceState = api;
})();
