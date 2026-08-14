# SA 代码评审

## 结论

APPROVED。P0=0，P1=0，可进入 GitHub-first 发布流程。

## 评审范围

- HTML/CSS 原生details布局、成功结果联动及响应式/RTL。
- 23语copy合并、静态locale和content-addressed JS构建。
- WebP内容hash、Nginx exact immutable location和CSP边界。
- bridge/browser测试、需求/部署/回滚文档。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | `tt-drama-code-search.js` | 初版使用`Object.fromEntries`提高旧Android WebView最低版本 | 改用`Object.keys`+`reduce` | 已关闭 |
| CR-002 | P1 | `deploy.md` | 初版写“仅引用一次”，与缩略图+完整图两处引用不一致 | 明确同URL两次引用并设置测试门禁 | 已关闭 |
| CR-003 | P1 | 静态资产合同 | 图片文件名可能与实际字节hash脱节 | SHA-256前12位、单一当前WebP、23语同URL断言 | 已关闭 |
| CR-004 | P2 | 23语文案 | 自动化可验证完整性和布局，不能替代母语审校 | 发布后按主要国家抽样校对 | 接受风险 |

## 编译 / 验证结果

2026-08-14 实际执行：

```text
node --check static/tt-drama-code-search.js                         PASS
node --check scripts/build_tt_drama_code_assets.js                 PASS
node scripts/build_tt_drama_code_assets.js --check                 PASS (23 locales, JS 45ea9a9af6ac)
node scripts/test_tt_drama_code_bridge.js                          PASS (233 assertions)
node scripts/test_tt_drama_code_browser.js                         PASS (107 checks, real Chrome)
git diff --check                                                   PASS
```

新 JS SHA-256：`45ea9a9af6aceeebfa1efcab7a65f38ff3defc17803291ba09c67afea52c6d8c`。
