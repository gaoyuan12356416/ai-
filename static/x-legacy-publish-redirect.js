/* Old bookmarks move to the supported workflow; generated tag pages stay put. */
(() => {
  const replacements = {
    "/x-auto-publish-templates.html": "/x-tag-auto-publish-templates.html",
    "/x-auto-publish-template.html": "/x-tag-auto-publish-templates.html",
    "/x-auto-publish-runs.html": "/x-post-logs.html",
    "/x-post-material-pool.html": "/x-tag-material-pool.html",
  };
  const target = replacements[window.location.pathname];
  if (target) window.location.replace(target);
})();
