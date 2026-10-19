importScripts('rules.js');
const RULE_IDS = [19001, 19002];
const EXPIRY_ALARM = 'x-sandbox-callback-expiry';

async function clearRules() {
  await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: RULE_IDS});
  await chrome.alarms.clear(EXPIRY_ALARM);
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === EXPIRY_ALARM) clearRules().catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  let url;
  try {url = new URL(sender.url);} catch (_) {reply({ok: false}); return;}
  if (url.origin !== 'http://127.0.0.1:18880' || url.pathname !== '/'
      || sender.frameId !== 0 || message?.source !== 'x-sandbox-content') {
    reply({ok: false}); return;
  }
  (async () => {
    if (message.kind === 'PING') return {ok: true, version: '1.0.0'};
    if (message.kind === 'CLEAR') {await clearRules(); return {ok: true};}
    if (message.kind !== 'REGISTER') return {ok: false};
    const plan = sandboxCallbackRules(message.payload || {});
    for (const rule of plan.rules) {
      const supported = await chrome.declarativeNetRequest.isRegexSupported({
        regex: rule.condition.regexFilter, isCaseSensitive: true, requireCapturing: true
      });
      if (!supported.isSupported) throw new Error('unsupported_callback_rule');
    }
    await chrome.declarativeNetRequest.updateSessionRules({removeRuleIds: RULE_IDS, addRules: plan.rules});
    await chrome.alarms.create(EXPIRY_ALARM, {when: plan.expires});
    return {ok: true};
  })().then(reply, () => reply({ok: false, error: 'callback_rule_registration_failed'}));
  return true;
});
