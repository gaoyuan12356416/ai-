# 2026-09-08 部署验收

已部署并启用。代码提交 `31aefc70b913ac46c54fc5f636fee97a4a51252e`，
分支 `codex/post-daily-report-20260908`，GitHub `gaoyuan12356416/ai-`。

CPU服务目录 `/opt/post-daily-report/current` 指向
`/mnt/data-disk/post-daily-report/releases/31aefc70b913ac46c54fc5f636fee97a4a51252e`。
服务器从GitHub拉取后核对提交，源文件使用Git blob的LF字节进行SHA256验证。

## 业务结果

2026-09-07：TT应发64/昨日确认48/补发0/未完成16；
FB50/50/0/0；X129/63/0/66。X往期计划昨日完成5，单列。

正式飞书消息于北京时间2026-09-08 11:44:18发送成功：

- 群：`oc_7c683c5770aef2e6c84a456e52cad389`
- `message_id=om_x100b66cc07d78ca4c1c0c2b4437b671`
- 发送返回code=0；随后GET消息读回code=0，chat_id精确匹配，interactive，deleted=false。
- delivery状态sent、attempts=1。11:45:15再次启动报表服务返回already_sent，未增加发送尝试。
- 卡片SHA256：`7e0e67242f8133de0f439f1094cf3cb3e6125ded5b7802c0f48737204e208ff7`；含转义content的请求预算16019字节。
- `post-daily-report.timer` enabled/active；下一次 **2026-09-09 10:00:00 Asia/Shanghai**。

## 验证

- 本地45项报表、去重、异常、跨日及审计测试全部通过。
- TT既有核心89项回归全部通过。
- Python语法与git diff --check通过；服务器systemd-analyze verify通过。
- 新版报表在应用审计前后均得到相同的64/48、50/50、129/63与往期5。
- TT/X/FB三库PRAGMA quick_check均为ok。
- TT审计初始8行；X审计初始2行；FB新快照表已创建，启用时0行，等待后续自然计划记录。
- TT/X触发器在线激活，同事务核对业务表数量未变；未重启TT、X、主API服务。
- FB入口排空后只重启fb-auto-post-service，health.ok=true；原本active的scheduler/plan/runner/reconcile四个timer均恢复active。
- fb-auto-post-prepare.timer在部署前已inactive，按原状态保留，未擅自启用。
- 未执行任何Post测试、重放、补发、停账号或发布规则调整。

## 备份与回滚

备份目录：`/mnt/data-disk/post-daily-report/backups/20260908T034341Z`。
`manifest.json`保存每个源文件备份、原/新SHA256、初始timer列表与恢复状态；
tt.sqlite3、x.sqlite3、fb.sqlite3为在线备份，仅供取证，禁止直接覆盖新发布账本。

暂停日报的精确命令：

```bash
systemctl disable --now post-daily-report.timer
```

恢复审计源代码的步骤：

1. 按manifest的files映射，将0.source恢复至TT sidecar core，1.source恢复X sidecar service，2.source恢复主API X service；这三个运行进程不需为回滚重启。
2. 若需恢复FB core，先停 `fb-auto-post-{scheduler,plan,runner,reconcile}.timer`，等待同名service全部inactive、FB task/due无lease且18835无established连接；将3.source恢复至FB core，重启 `fb-auto-post-service.service`，确认 `/health` 的ok=true后恢复原四个timer。健康失败时保持这些入口暂停并处理回滚。
3. 停止后续配置审计只需在对应SQLite事务中删除以下具名触发器；保留审计表和已生成报表。

```sql
-- TT库
DROP TRIGGER IF EXISTS tt_post_schedule_audit_insert;
DROP TRIGGER IF EXISTS tt_post_schedule_audit_update;
-- X库
DROP TRIGGER IF EXISTS trg_x_post_schedule_config_audit_insert;
DROP TRIGGER IF EXISTS trg_x_post_schedule_config_audit_update;
```

不能修改delivery的sent/unknown状态来强制重发。已发送群消息和最新发布事实不随代码回滚撤回。

个人记忆未修改；运行约定与验收证据保存在本需求文档中。
