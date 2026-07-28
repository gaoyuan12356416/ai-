# SA 代码评审

## 结论

通过。两轮独立静态复核后未发现剩余 Blocker / High，可进入 GitHub 提交和生产验收。

## 评审范围

- 独立 Bearer Token 鉴权和 Token 轮换
- HTTP 输入边界、幂等和 SQLite outbox
- `admin_users -> admin_user_group.email` 查询安全
- 飞书用户解析、私聊、兜底、重试和去重
- worker 就绪、租约和服务重启行为
- Nginx 精确路由、32 KiB 限制和 413 JSON
- Token、email、open_id 等敏感信息保护

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | High | `app.py` 优化师查询 | 手工 SQL 字符串转义受 MySQL sql_mode 影响 | 名称改为 UTF-8 hex 表达式，并以 `BINARY TRIM` 做大小写敏感精确匹配 | 已修复 |
| CR-002 | High | `app.py` 飞书发送 | 发送成功但本地确认丢失时可能重复 | 私聊/兜底分别使用稳定 UUID，并固定兜底重试阶段 | 已修复 |
| CR-003 | High | `app.py` 飞书响应 | 缺少 `code` 或 `message_id` 可能被误记成功 | 只接受 `code=0` 且 `message_id` 非空 | 已修复 |
| CR-004 | High | Token 校验 | 非 ASCII Authorization 可令字符串恒定比较抛错断连 | 转为 ASCII bytes 后比较，编码失败统一返回 `401` | 已修复 |
| CR-005 | High | worker 生命周期 | worker 启动失败时接口可能仍返回 `202` | 入队前执行 readiness 门禁并按请求自愈，失败返回 `503` | 已修复 |
| CR-006 | High | 飞书 Token 缓存 | Token 失效后重试可能持续复用旧缓存 | 识别鉴权错误码，条件清缓存并即时刷新一次 | 已修复 |
| CR-007 | Medium | Nginx 413 | 代理层超限可能返回 HTML 而非接口 JSON | exact location 增加专用 `error_page 413` JSON | 已修复 |

## 编译 / 验证结果

- Python 3.9 grammar：通过
- `py_compile`：通过
- 需求专项测试：28/28 通过
- 相关既有回归：71/71 通过
- `git diff --check`：通过
- 最终静态复核：无 Blocker / High

生产 MySQL、飞书、Nginx effective config 和真实群播报证据记录在测试报告与部署验收中。
