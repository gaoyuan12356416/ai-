# 部署文档

## 变更内容

新增定向恢复脚本；现有 store 建单能力增加受限白名单和模板版本锁。无 HTTP 路由和 schema 变化。

## 配置项

沿用 `/etc/fb-auto-post.env`。恢复报告根目录固定为 `/mnt/data-disk/fb-auto-post-publisher/recoveries`；无需新增密钥。

## 数据库变更

无 DDL。apply 会新增 1 条 `fb_auto_run`、5 条 `fb_auto_run_page` 和 5 条 `fb_auto_task`，后续由现有状态机写 attempt/ledger。

## 部署步骤

1. 本地完成测试、代码评审并提交推送 GitHub。
2. 生产确认数据盘 UUID/空间/可写、服务健康、无到期积压。
3. 使用 SQLite online backup 备份操作库并执行 `quick_check`，记录 SHA-256。
4. 从 GitHub 获取精确 commit 到新的不可变 release 目录，安装与原 release 相同的权限/依赖。
5. 原子切换 `/opt/fb-auto-post/current`，仅重启 `fb-auto-post-service.service`，验证健康与 timers。
6. 以固定来源和 Page 集合执行 validate-only，保存指纹；核对无范围漂移后 apply。
7. 观察现有 prepare/execute/reconcile timers 至新 run 终态，产出测试报告。

## 验证步骤

- `git rev-parse HEAD` 与 GitHub commit 一致，服务 active、NRestarts 稳定。
- dry-run 的来源 run/date/5 Page/实时 Token 计数/既有 recovery 数量正确。
- apply 新 run 恰好 5 个 Page；另外 8 个今日已发布 Page 计数不变；run 21 不变。
- 最终逐 Page 检查 task、attempt、ledger、Graph post id 和 unknown_outcome。

## 回滚方案

代码回滚：把 `current` 原子指回上一个 release 并重启单一服务。数据回滚：apply 前可直接恢复 SQLite 备份，但仅允许在确认没有任何外部 Graph 成功/未知结果后执行；一旦外部发布已发生，禁止用数据库回滚伪造未发布状态，应保留账本并人工处置。

## 注意事项

- dry-run 不是发布；apply 建单也不是最终发布成功。
- 不运行整模板 `run-now`，不手工重开历史 skipped 任务，不清除 unknown。
- 生产报告不得包含 Token、完整消息文本、长短链或其他凭证信息。
