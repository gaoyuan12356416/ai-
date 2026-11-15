const fs = require('fs'), assert = require('assert'), vm = require('vm');
const code = fs.readFileSync(require('path').join(__dirname,'../static/x-post-logs.js'),'utf8');
const match = code.match(/const safeShortUrl = value => \{([\s\S]*?)\n      \};/);
assert(match);
const context = {URL};vm.createContext(context);
vm.runInContext('safe = value => {' + match[1] + '};',context);
for(const u of ['https://gy.g2flow.com/s/1504','https://ai.yingliangads.com/s2l/12.html']) assert.equal(context.safe(u),u);
for(const u of ['http://gy.g2flow.com/s/1','https://gy.g2flow.com.evil.test/s/1','javascript:alert(1)',
  'https://evil.test/s/1','https://user:secret@gy.g2flow.com/s/1','https://gy.g2flow.com:444/s/1','https://gy.g2flow.com/other/1']) assert.equal(context.safe(u),'');
console.log('9 short-link allowlist checks passed');
