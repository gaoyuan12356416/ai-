# Facebook 发布 / Page 组文案原型 · 2026-10-19

本版从 `46b92f8` 的独立静态原型继续调整。仅新增本目录，保留 2026-10-13 原型、原 COS 地址和所有生产模块。

## 本版交互

- 按现有 YouTube / AI 后台的深色侧栏、共用 QuickNav / UiTopbar、字号、按钮、卡片、表格和表单收敛视觉。修正旧原型浅色导航覆盖及直达页面选中项。
- `index.html` 为发布入口，`page-groups.html` 直接打开 Page 组管理；三个视图均可通过导航 / 页签切换。
- 三个现有示例组：中文、英语、西语。组名和成员只读，可按组名、成员主页搜索并筛选已配置 / 未配置文案。
- 编辑组文案支持 `{name}` / `{desc}` / `{url}`，点击插入光标处、素材切换预览、未知宏 / 双层花括号校验、替换后 5000 UTF-8 字节上限。
- 文案保存到浏览器；刷新恢复，未保存关闭保留 / 放弃提示。模板留空时不覆盖发布表单当前文案。
- 选择组带入该组可用主页及文案；已有不同文案需确认覆盖，授权异常主页被跳过。单次发布仍可修改。
- 模拟任务冻结替换后的文案及模板、组 ID、landingId=2049；后续组模板编辑不改写已创建任务。

## 宏与链接来源

宏语法核对 `features/youtube_auto_publish/templates.py` 和 `static/youtube-channels.js`（工作副本 `youtube-channel-cover-20261015`），是单层花括号的 `{name}`、`{desc}`、`{url}`。

2049 目标沿用 FB 链接契约：`https://www.dramawavew2a.com/ads/0/2049/view`。原型用 `demo-content-*` 与 `af_channel=AIpost` 展示链接，明确标为示例，不生成或分配真实短链、不宣称已完成真实归因集成。

## 边界

全部 Page 组、主页、素材与任务均为模拟数据；仅发布 `dist/` 静态文件。保存 key 前缀 `facebook-page-group-preview-v2-20261019`。不查询真实 Page 组，不调用后端或 Facebook API，不处理 / 上传本地视频，不创建生产任务。

正式接入时仍需实现租户、权限、真实组目录、模板版本并发校验、逐素材预检及冻结链接契约；模拟身份不是认证方案。

## 验证与回退

见 `VERIFICATION.md`。本地可执行 `python -m http.server 8779 --bind 127.0.0.1 --directory dist`。

分支 `codex/fb-page-group-preview-20261019`；原版回退点 `46b92f8`，直接继续使用原 COS 链接即可。生产服务、数据库、发布账本无变更，不需要服务回滚。

技能上下文保持不变：这是静态需求原型，未改变后端规则或运维流程。
