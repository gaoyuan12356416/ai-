const assert = require('node:assert/strict');
const { inRange } = require('./stage-age-range.js');
for (const age of [6, 7, 12, 13]) assert.equal(inRange(age), true);
for (const age of [null, undefined, '', '6', NaN, Infinity, -1, 0, 5, 14, 15, 6.5]) assert.equal(inRange(age), false);
console.log('第7—14天边界、未知和未来时间检查通过');
