# 测试报告

## 测试结论

本地离线阶段通过；真实 X 灰度与生产短链尚待部署后执行。

## 测试范围

- X Post 业务模块、账号 sidecar 集成、App 路由契约和 legacy owner backfill。
- 所有上游 HTTP 在本地测试中均为 mock，未由测试代码调用真实 X。
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

## 遗留风险

- X API 的账号产品层权限只能由真实上传/Create Post 验证。
- Create Post 结果不确定时系统会停止且不重试，需要人工查看账号后处理。
- 生产 Nginx、systemd sandbox、SQLite/Token 保全和公开短链仍需现场验收。

## 发布建议

满足部署前备份和副本迁移门槛后，允许仅对账号 `ShortsDramhx`、素材 `5221348` 执行一条真实灰度；不得启用 timer/cron 或第二账号发布。
