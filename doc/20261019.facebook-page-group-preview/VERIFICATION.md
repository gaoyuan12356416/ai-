# 验证记录 · 2026-10-19

## 本地

- Node 语法检查：app.js、nav-config.js、quick-nav.js 通过。
- Playwright CLI 实际浏览器 39 项检查通过：直达 Page 组页、搜索成员、空状态、文案状态筛选、异常主页显示、2049 URL、光标处宏插入、非法 / 双花括号拒绝、UTF-8 上限、纯文本安全预览、切换素材替换、保存 / 刷新、关闭时保留 / 放弃、发布组套用 / 切换 / 空组保留、草稿恢复、确认文案、独立模拟任务、历史文案不被新模板改写、清空模板。
- 1440×1050、820×1050、390×844：发布页与组页无整页横向溢出，编辑弹窗在视口内；桌面表格在手机独立横向滚动。
- 浏览器 0 错误 / 警告，44 个请求均为同源静态 GET，无 /api/ 或第三方请求。
- 截图视觉复核通过。修正原浅色导航 !important 覆盖后，活动项为白字蓝底，字号 14px；直达组页 activeKey=fbGroups。
- 测试脚本 / 截图保存在被忽略的 output/；自动化中的原生 confirm 选择使用测试适配，页面保留原生确认。清理本地演示状态后交付。

## COS

- 发布页：https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-group-preview-20261019_122013/index.html
- Page 组页：https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20261019/facebook-page-group-preview-20261019_122013/page-groups.html
- 静态资源来源提交：`be12a7910dcc72781d6bce854eb959059ba4b144`，已推送 `codex/fb-page-group-preview-20261019`。
- 13 个文件均 HTTP 200，长度及 SHA-256 与本地一致；HTML HEAD `200 / text/html / 21992 bytes`。
- index.html SHA-256：`720452dac1b1bdc10985261b6866ff9613a0691194a2050e5fbfdaa7a386d6e7`。
- 公网重复上述 39 项浏览器检查全部通过，44 个同源静态 GET、0 浏览器错误 / 警告；无后端或 Facebook 请求。
- 检查结束仅清理本原型独立 localStorage 前缀，公网以初始示例组呈现。
- 保留旧 COS 版本；回退到原链接或 Git `46b92f8` 的 `doc/20261013.facebook-publishing-preview/dist/` 即可。生产服务、数据库无变更。
- 技能上下文未变。
