(() => {
  // Match patterns cannot restrict ports. Enforce the exact test origin here.
  if (location.origin !== 'http://127.0.0.1:18880' || location.pathname !== '/') return;
  const send = (kind, payload) => chrome.runtime.sendMessage({source: 'x-sandbox-content', kind, payload});
  if (new URL(location.href).searchParams.has('oauth')) send('CLEAR').catch(() => {});
  window.addEventListener('message', event => {
    const message = event.data;
    if (event.source !== window || event.origin !== location.origin
        || message?.source !== 'x-sandbox-page'
        || !['PING', 'REGISTER'].includes(message.kind)
        || typeof message.id !== 'string') return;
    send(message.kind, message.payload).then(
      result => window.postMessage({source: 'x-sandbox-extension', id: message.id, result}, location.origin),
      () => window.postMessage({source: 'x-sandbox-extension', id: message.id, result: {ok: false}}, location.origin)
    );
  });
})();
