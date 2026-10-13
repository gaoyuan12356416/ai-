# Facebook 发布交互原型 · 2026-10-13

依据用户提供的 `facebook-publishing-requirements.md` 制作，用于确认视觉与交互后再决定正式开发。此目录是独立静态演示，不注册正式模块，不修改生产导航、权限、发布器或数据库。

## 需求映射

- 选择视频：模拟素材库、搜索、语言筛选、单选；附加本地视频预览，仅创建浏览器 Object URL，不上传文件。
- 选择多个 Facebook 发布账号 / 主页：多选、搜索、全选、不可用主页示例；每个主页独立演示任务。
- 固定去重模板：3 个待确认占位模板与不去重选项。名称与处理方式仅用于评审，不执行视频处理、不声明去重效果。
- 填写文案：统一文案、逐主页切换实时预览、本地草稿。
- 立即或定时发布：未来时间校验、明确北京时间 UTC+8、确认面板。立即发布由浏览器定时器演示进度；预约只保存演示状态，不在到期后执行。
- 不包含封面生成或审核。SVG 仅作为示例素材画面的视觉占位，与封面功能无关。

## 边界与组件来源

复用现有 `static/quick-nav.js`、`static/ui-topbar.js` 与 `static/ui-topbar.css`。原型的 QuickNav 副本仅增加显式 `window.FB_PREVIEW_NAV` 离线入口，且不持久化模拟身份；生产文件保持原样。
顶栏由 `UiTopbar.render` 接收明确的模拟身份。COS 页面没有正式登录 / 退出功能，不请求 `/api/ui/topbar` 或任何后端 / Facebook API。
正式开发时需恢复 Feishu 会话、`/api/ui/topbar` 与正式导航权限，不可把模拟身份当作认证方案。

发布目录仅 `dist/`；不上传源需求、README、验证结果或工作区内容。素材、主页、任务均为虚构示例。
浏览器持久化 key 前缀：`facebook-publishing-preview-v1-20261013`。只保存文本配置与演示记录，不保存本地视频；存储失败时保留当前内存状态。

## 本地运行与检查

```powershell
python -m http.server 8768 --bind 127.0.0.1 --directory dist
node --check dist/assets/app.js
node --check dist/assets/nav-config.js
node --check dist/assets/quick-nav.js
```

开发分支 `codex/fb-publishing-preview-20261013`，来源 `9a91402`。整个变更只增加此目录，可从分支恢复任何已提交原型版本；正式后台没有部署或回滚动作。
技能上下文保持不变：本次是需求评审原型，没有产生新的后端行为或运维规则。
