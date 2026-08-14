# 部署文档

## 变更内容

授权/Access Token 状态分离、素材/短剧/人工排期预检刷新、X Auto 真实任务刷新、最终写入保护及两套账号 UI。

## 配置项

无新增配置；复用现有 X OAuth Client、Sidecar、daily/auto internal bearer 与 Token 目录。

## 数据库变更

无 schema 或数据迁移。历史账号、Run、Task、Queue、Log、Post 不重写。

## 部署步骤

1. 推送 `codex/x-offline-token-refresh-20260814` 精确提交。
2. 校验生产基线 release `7ed5f203e028129f88afbc675da9237326bfd364` 与 Git 文件哈希一致。
3. 在线备份 X/X Auto SQLite、Token 目录、代码、两处静态页、units/env 元数据和定时器状态。
4. 从 GitHub 精确提交创建不可变 release，在 release 内跑编译与聚焦测试。
5. 停止 Sidecar/X Auto，原子切换 `/opt/x-post-automation/current`，同步主应用静态和 Nginx docroot。
6. 启动并健康检查 Sidecar/X Auto；主 API 代码未变则不重启。
7. 验收后按部署前 enabled/active 状态恢复 timers/path。

## 验证步骤

- Sidecar `/health`、X Auto health、主 API/Nginx 页面返回 200。
- 安全账号 DTO：17 个可续期账号均 `status=active`、`access_token_expired=true`、`authorization_refreshable=true`；不输出 Token。
- 页面文案与静态哈希一致，匿名写接口仍 401/403。
- 两个 SQLite `quick_check=ok`，FK 无异常。
- Queue/Log/Published/unknown、X Auto Run/Task/Ledger 与部署前一致。
- Token 数量、mode/owner/组合哈希在未发生自然刷新前一致；若自然刷新发生，只接受审计到的预期轮换，不恢复旧备份。

## 回滚方案

停止发布 timers/path 与受影响服务，切回 `7ed5f20`，恢复两处静态页和 unit 文件，重启 Sidecar/X Auto，再按原状态恢复触发器。默认保留当前 SQLite 与 Token；刷新发生后严禁用备份 Token 覆盖。

## 注意事项

不运行 `run-now`、不手动启动发布 oneshot、不创建 canary Post；验证依赖 mock/离线、安全 DTO 和自然 timer 结果。
