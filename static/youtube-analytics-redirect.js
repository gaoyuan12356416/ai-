(() => {
  'use strict';
  const query = new URLSearchParams(location.search);
  if (!query.has('platform')) query.set('platform','youtube');
  const destination = '/social-analytics.html?' + query.toString() + location.hash;
  const link = document.getElementById('social-report-link');
  if (link) link.href = destination;
  location.replace(destination);
})();
