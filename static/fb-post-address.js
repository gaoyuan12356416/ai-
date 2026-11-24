(() => {
  "use strict";
  function postAddress(permalink, objectId) {
    if (permalink) {
      try {
        const u = new URL(permalink);
        if (u.protocol === "https:" && !u.username && !u.password && !u.port &&
            ["facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"].includes(u.hostname)) return u.href;
      } catch (_) { /* Fall back to the platform receipt. */ }
    }
    const id = String(objectId || "");
    // Both FB publishers submit Page videos and persist Meta's video object ID.
    if (/^[1-9]\d{0,30}$/.test(id)) return "https://www.facebook.com/watch/?v=" + id;
    const post = /^([1-9]\d{0,30})_([1-9]\d{0,30})$/.exec(id);
    return post ? "https://www.facebook.com/" + post[1] + "/posts/" + post[2] + "/" : "";
  }
  window.FBPostAddress = postAddress;
})();
