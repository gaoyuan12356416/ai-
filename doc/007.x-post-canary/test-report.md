# 测试报告

## 测试结论

本地、生产部署与单条真实 X 灰度均通过。账号 `ShortsDramhx` 已成功发布素材 `5221348`，公开 Post、短链和 W2A 落地页均返回 200，发布日志状态为 `published`。

## 测试范围

- X Post 业务模块、账号 sidecar 集成、App 路由契约和 legacy owner backfill。
- 自动化测试中的上游 HTTP 均为 mock；真实 X 仅在用户授权的单条生产灰度中调用一次。
- 生产候选另行完成只读 SQL、HEAD/Range 与 ffprobe 审计。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| X Post 模块 | 14 | 14 | 0 | 0 |
| X 账号 sidecar | 32 | 32 | 0 | 0 |
| App contract | 5 | 5 | 0 | 0 |
| Owner backfill | 4 | 4 | 0 | 0 |
| 合计 | 55 | 55 | 0 | 0 |

## 缺陷情况

- 评审发现的真实素材名方括号和 2xx `errors[]` 两个边界已在提交前修复并补回归。
- 当前无开放自动化测试缺陷。

## 验证证据

- `py_compile` 全部通过。
- 55 项 unittest 全部通过。
- `git diff --check` 通过，仅 Windows 工作树提示未来可能 CRLF 转换，不属于内容错误。
- [candidate-audit.md](candidate-audit.md) 证明 `5221348` 的当日消耗顺序、违规/危险标签 0 命中及真实视频规格。
- 使用最终模块对真实候选 URL 完整下载并执行其内置 ffprobe 门禁：`42,312,248` bytes、SHA-256 `ee1001198dc7fe3044112f1cecc7ca8ed5bac700024f6df9a81b4e6d3ad47596`、H.264/yuv420p/AAC、`720x1280`、30fps、68.708005s，全部通过。
- 生产副本迁移、生产增量迁移和生产精确 release 上的 55 项回归均通过；部署代码 commit 为 `cd119e248334be427507a8242a2e3c55dbb5269d`。
- Queue `1` / log `1`：一次尝试成功，`unknown_outcome=0`、无错误；X post ID `2080128600917905497`，media ID `2080128441161031680`。
- 公网视觉验收显示账号、短链、完整描述和竖版视频均正常；DOM 中视频处于可播放状态，公开预览为 <https://x.com/ShortsDramhx/status/2080128600917905497>。
- `https://ai.yingliangads.com/s2l/1.html`、其精确 W2A 长链和 X 预览均返回 200；短链 HTML 与日志长链完全一致。
- 发布后 `/users/me` 同步结果：账号 active、publish eligible，`tweet_count=1`、`media_count=1`。
- sidecar 为 active/running，`Restart=always`、`NRestarts=0`；本地/公网 health 均为 200，公网 internal route 为 404。
- DB 与 journal 对 `access_token`、`refresh_token`、`Authorization: Bearer` 的敏感字段检查均为 0；未安装匹配本功能的 timer/cron。

## 遗留风险

- Create Post 结果不确定时系统会停止且不重试，需要人工查看账号后处理；本次实际结果明确为成功，不触发该分支。
- 当前只验证了账号 `ShortsDramhx` 和素材 `5221348`；其余账号与每日调度尚未启用或验证。

## 发布建议

单条灰度已完成。等待用户确认预览效果前，不启用 timer/cron，不向第二账号发布，也不重复发布该素材。
