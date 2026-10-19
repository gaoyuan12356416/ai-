/* No credentials or network calls. Build two short-lived exact-state rules. */
function sandboxCallbackRules(payload, now = Date.now()) {
  const auth = new URL(String(payload.authorization_url || ''));
  const official = 'https://ai.yingliangads.com/x-oauth/callback';
  const state = auth.searchParams.get('state');
  const guard = String(payload.callback_guard || '');
  const expires = Date.parse(payload.expires_at);
  if (auth.origin !== 'https://x.com' || auth.pathname !== '/i/oauth2/authorize'
      || auth.searchParams.get('redirect_uri') !== official
      || !/^[A-Za-z0-9_-]{32,128}$/.test(state || '')
      || !/^[A-Za-z0-9_-]{32,128}$/.test(guard)
      || !Number.isFinite(expires) || expires <= now || expires > now + 610000) {
    throw new Error('invalid_sandbox_authorization');
  }
  const query = '((?:[^#]*&)?state=' + state + '(?:&[^#]*)?)$';
  return {
    expires,
    rules: [
      {id: 19001, priority: 100, action: {type: 'redirect', redirect: {
        regexSubstitution: 'http://127.0.0.1:18880/x-oauth/callback?\\1'
      }}, condition: {
        regexFilter: '^https://ai\\.yingliangads\\.com/x-oauth/callback\\?' + query,
        isUrlFilterCaseSensitive: true, resourceTypes: ['main_frame']
      }},
      {id: 19002, priority: 100, action: {type: 'modifyHeaders', requestHeaders: [
        {header: 'X-Sandbox-Callback-Guard', operation: 'set', value: guard}
      ]}, condition: {
        regexFilter: '^http://127\\.0\\.0\\.1:18880/x-oauth/callback\\?' + query,
        isUrlFilterCaseSensitive: true, resourceTypes: ['main_frame']
      }}
    ]
  };
}
if (typeof module !== 'undefined') module.exports = {sandboxCallbackRules};
