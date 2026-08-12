# API 文档

本需求不新增、不删除、不改名任何 HTTP API 字段。

行为变化仅限正式 X 素材池的内部选材和媒体预检：

- `duration <= 140s`：所有当前可发布账号可用。
- `duration > 140s && duration <= 14400s`：仅 Token 当前确认 `basic|premium|premium_plus` 的账号可用。
- 其他资格缺失或过期：在 X 写入前失败关闭。
- X 自动发布模板的独立内部接口继续执行 600 秒上限。
