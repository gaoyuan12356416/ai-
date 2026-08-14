# 测试用例

## 测试范围

引导首屏、展开/收起、搜索状态联动、23 语静态产物、响应式/RTL、图片缓存和原有 Search/Featured 回归。

## 测试数据

- 视口：320×568、390×844、720×900。
- locale：en-US、zh-CN、zh-TW、ar；其余 locale 由静态 copy 完整性断言覆盖。
- 搜索：合法 4 位 code、完整 Content ID、无效输入。
- 图片：`tt-code-location-guide.0b42fbc64ab4.webp`，720×960。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 默认首屏 | 打开 `/tt` | 检查 guide | 折叠、缩略图/本地化文案可见 | P0 | 通过 |
| TC-002 | 展开示例 | guide 折叠 | 点击 summary | details open，完整图720×960可见 | P0 | 通过 |
| TC-003 | 键盘/原生语义 | guide 聚焦 | Enter/Space切换 | 可展开/收起且无需自定义弹窗脚本 | P1 | 通过 |
| TC-004 | 搜索成功联动 | guide 已展开 | 搜索合法 code | guide 收起并隐藏，结果 CTA 可见 | P0 | 通过 |
| TC-005 | 搜索失败 | 初始页面 | 搜索无效/不存在 code | guide 保留，不阻断错误提示 | P0 | 通过 |
| TC-006 | 输入重置 | 已有成功结果 | 修改输入 | 结果隐藏，guide 以折叠状态恢复 | P1 | 通过 |
| TC-007 | 移动端 | 320×568、390×844 | 展开 guide | 无横向溢出、图片比例稳定 | P0 | 通过 |
| TC-008 | 桌面端 | 720×900 | 展开 guide | page max-width 内无遮挡，Featured顺序正常 | P1 | 通过 |
| TC-009 | RTL | `Accept-Language: ar` | 打开页面 | 标题阿语、布局RTL、图片保持原方向 | P0 | 通过 |
| TC-010 | 23语静态构建 | 运行build --check | 检查locale HTML | copy key齐全、无i18n marker、同一图片URL | P0 | 通过 |
| TC-011 | 图片失败隔离 | 阻断 guide WebP | 使用搜索 | 搜索/Featured仍可用，无布局崩塌 | P0 | 通过 |
| TC-012 | immutable资源 | 部署后 | GET hashed WebP | 200、image/webp、一年immutable | P0 | 通过 |
| TC-013 | Search归因 | 合法4位code | 搜索并读target | `af_channel=TT`，八字段不变 | P0 | 通过 |
| TC-014 | Featured归因 | 点击推荐卡 | 拦截target | `af_channel=Featured`，resolver先验证 | P0 | 通过 |
| TC-015 | 双入口/路径合同 | 请求三个路径 | GET `/tt`、`/tt-code`、`/tt/` | 前两者200，尾斜杠仍404 | P0 | 通过 |

## 回归范围

- `scripts/build_tt_drama_code_assets.js --check`
- `scripts/test_tt_drama_code_bridge.js`
- `scripts/test_tt_drama_code_browser.js`
- `scripts/smoke_tt_drama_code_production.js`
- Nginx `nginx -t`、真实 Chrome、Resolver/Featured 目标参数。
