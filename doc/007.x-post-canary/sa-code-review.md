# SA 代码评审

## 结论

有条件通过生产灰度。代码与单元回归已通过；生产前仍必须完成数据库/Token 备份、生产副本迁移、Nginx 静态映射和 systemd sandbox 内 ffprobe/health 验证。

## 评审范围

- `features/x_posts/service.py` 与公开导出。
- `features/x_accounts/oauth_service.py` canary 路由、账号刷新/锁边界、返回脱敏。
- `deploy/x-post-automation.service` 写路径收敛。
- 55 项自动化测试和候选审计证据。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | X Create Post | 网络/5xx/成功结构异常可能已创建帖子 | 标记 unknown、落库并禁止任何自动重试 | 已修复 |
| CR-002 | P0 | sidecar 发布入口 | 客户端可伪造用户名/page，造成归因错误 | 发布锁内用账号库动态资料覆盖所有账号字段 | 已修复 |
| CR-003 | P0 | 短链 | 任意目标会形成开放重定向 | 目标严格限定 HTTPS Dramawave 固定路径和精确参数序列 | 已修复 |
| CR-004 | P0 | 素材下载 | 重定向/任意域名可能 SSRF 或泄露 | HTTPS、精确 host allowlist、禁止重定向、大小/MIME/ffprobe 门禁 | 已修复 |
| CR-005 | P1 | 静态文件权限 | sidecar `UMask=0077` 会让 Nginx 无法读取 | 短链目录/HTML 显式 0755/0644，媒体工作目录保持私有 | 已修复 |
| CR-006 | P1 | 生产 ffprobe | 系统 PATH 无 ffprobe | 支持 `X_POST_FFPROBE_BIN`；部署绑定已核 hash 的静态二进制路径 | 已修复，待生产 sandbox 验证 |
| CR-007 | P1 | SQLite 兼容 | 不能破坏 OAuth 既有表/Token | 只增新表/索引；同库事务、0600，生产副本再演练 | 已修复，待部署演练 |
| CR-008 | P1 | 内部入口幂等 | 调用方自定义 key 可绕过固定防重 | 忽略客户端 `idempotency_key/queue_id`，仅由账号+日期+素材生成 | 已修复并回归 |

## 编译 / 验证结果

```text
python -m py_compile ...                                    PASS
python scripts/test_x_posts.py                              14/14 PASS
python scripts/test_x_accounts.py                           32/32 PASS
python scripts/test_x_accounts_app_contract.py               5/5 PASS
python scripts/test_x_account_owner_backfill.py               4/4 PASS
git diff --check                                              PASS
```

没有 P0/P1 开放代码问题；生产验收结果在部署后补入测试报告。
