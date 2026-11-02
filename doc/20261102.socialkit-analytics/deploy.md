# 部署文档

## 变更内容

独立 SocialKit 报表、授权模块 `socialkit_analytics`、本地查询快照、每日采集服务和查询技能。当前状态：代码及公共代理已部署，首次 14 日完整快照已发布，日 timer 已启用。56 个独立用例分组验证通过；已部署最终查询的 795 项真实检查和 54 次性能测量通过，最慢 1.7669 秒。

线上共享 app.py 与 Git 基线可能不同，安装器在当前线上文本中应用必要锚点补丁，并先生成备份与哈希清单；不整份覆盖共享源码。

## 已执行记录（2026-11-02）

- 分支 `codex/socialkit-analytics-20261102`；最终代码提交 `86e0b0dca9d01719121251334ced099797157211` 已推送 GitHub、CPU checkout 并安装，优化本地查询与技能 CSV 字节传输；保留 3a7d3de 已移除的页面日内轮询。采集实现为 `c0de95687efb71eee055483734f53512055452aa` 的逐日 Gate 和有限瞬时读重试；此前 `85b2150` 补齐 Nginx 公共 API 转发。
- 主 API active，最终观测 PID 1399003；Nginx 配置测试和 reload 成功，公共 options 匿名请求返回 401。
- 初期三份安装备份清单分别位于数据根 `backups/20261102T102020Z`（初版）、`backups/20261102T102333Z`（公共代理）、`backups/20261102T102707Z`（逐日 Gate）。这些保留审计；本次实际回滚使用下文最新合并包，不直接恢复旧共享 app/nav。
- 旧回填批次由执行者主动 TERM 停止，以更换逐日 Gate 读取方式；不把该停止记录算作成功快照。r2 首日读取失败，未发布部分数据；r3 使用显式日期 2026-10-19—2026-11-01 完成全部日期并返回 success/0。
- 已发布版本 `20261102T103831Z-b3498a15452c`，更新时间 2026-11-02 19:00:43 北京时间，14 日期的收入与发布全部控制量通过；逐日清单与已安装技能查询一致，业务总额仅保留于私有验收文件和服务器快照。当前/预计算已完成；日 timer active/enabled，下一次 2026-11-03 11:00:00 CST。最终真实性能 25 种分组/筛选及首页/选项共 54 次测量均低于 2 秒，最慢 1.7669 秒；口径和方法见 test-report.md。

## 配置项

- 页面：`https://ai.yingliangads.com/socialkit-analytics.html`；API 前缀 `/api/socialkit-analytics/`。
- 应用：`/root/drama_material_service`；静态文件同时安装至该目录 static 和 `/usr/share/nginx/html`。
- 公共 API 转发：`/etc/nginx/default.d/socialkit-analytics.conf`；只验证 loopback API 不足以确认公共路由。
- 发布指针：`/opt/socialkit-analytics/current`；数据根：`/mnt/data-disk/socialkit-analytics`。
- 私有环境：`/etc/socialkit-analytics.env`，权限 0600，复用现有 source/reader/writer 配置；文档和 Git 不保存值。由 systemd EnvironmentFile 解析，不用 shell 展开或直接打印文件。
- 数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。刷新独立服务 `socialkit-analytics-refresh.service`、timer 同名；11:00 Asia/Shanghai，Persistent=true，单实例锁。

## 数据库变更

仅在 `101.32.56.53:63353/ads_ai` 创建 `socialkit_effect_daily`、`socialkit_publish_daily`、`socialkit_refresh_runs`。金额整数分，版本+行键主键；运行刷新无 DDL。内部源读 63350 并校验 read_only=1，源任务/短链连接使用只读事务；不改业务发布记录或令牌。

## 部署步骤

以下为可复现执行清单；已完成状态以本文件“已执行记录”为准：

1. 将审查代码提交并推送 GitHub，CPU 拉取该精确提交到干净 release；核对 commit、工作区和必要依赖（Python 3.9、PyMySQL、mysql SQL Gate）。
2. 在 release 运行 `python3 ops/socialkit-analytics/install.py --prepare-env`。通过 systemd 临时单元加载 EnvironmentFile 后运行该脚本的 `--migrate`，不得把密码写到命令行。
3. 运行 `python3 ops/socialkit-analytics/install.py --install <commit>`；保存返回的 backup/release/commit。重启 `drama-material-api.service`，核对 health 和新增匿名 API 的 401。
4. 通过 systemd 临时单元加载同一 EnvironmentFile、WorkingDirectory 指向 release，运行 `python3 -m features.socialkit_analytics.refresh --root /mnt/data-disk/socialkit-analytics --start 2026-10-19 --end 2026-11-01`。每天的事实与控制 SQL 共享只读 RR，完整读取并释放 Gate 后才外源归属/写仓；所有日期/控制量/63350 验证完成后发布。
5. 检查 current/previous、SQLite 与 precomputed 首页/选项 JSON，核对完整日期和页面/API/导出同版本。
6. 验收通过后 `systemctl enable --now socialkit-analytics-refresh.timer`，核对下一次北京时间 11:00 和持久补跑。刷新不新增飞书通知。

## 验证步骤

- 服务器编译、执行本功能测试，核实 Python 3.9 实际运行。
- 14 日每日源收入=已归属+未归属，金额/退款/点击/安装/发布控制数一致；所有收入分组加总守恒。
- 未登录 401、无授权 403；管理员和获模块授权用户可见全部数据。既有导航组合权限保持。
- 已发布版本绑定筛选、图表和 CSV；刷新仅加载快照。零收入未归属仍提示，未覆盖日期不得伪造零。
- 实测首页及常见三维筛选耗时，目标 2 秒；记录数据规模、冷/热查询和版本。
- 技能调用生产已发布快照，记录版本/日期/总量，不输出凭据。

## 回滚方案

- 本次代码回滚使用专用合并包，单条命令如下（只读审查通过，未实际执行回滚）：

```bash
python3 /mnt/data-disk/socialkit-analytics/releases/86e0b0dca9d01719121251334ced099797157211/ops/socialkit-analytics/install.py --rollback /mnt/data-disk/socialkit-analytics/backups/before-socialkit-86e0b0d/manifest.json
```

- 合并包保留其他线程后来新增的 pipeline monitor，仅撤回 SocialKit 集成。独立只读复核 20 个当前文件均匹配 `new_sha256`；app 和两份 nav 的备份均匹配 `old_sha256`。app 通过 AST 与逆向重放逐字校验，两份 nav 通过 Node 语法及精确删除 SocialKit 两片段后的逐字校验，各自 8 条 pipeline 标记保留。普通早期备份会因共享文件后续变化而拒绝哈希预检，不应强制跳过保护。
- 安装器仍先检查所有当前哈希，全部通过后停本功能 timer、恢复文件并重新加载 Nginx/重启主 API。如果未来再有文件变化，应重新审查合并包；不要覆盖其他线程的修改。
- 查询快照回退：执行同脚本 `--rollback-snapshot`，获取数据根 refresh.lock 后检查 current/previous 数据文件，再交换两份指针；不重新采集、不改中间事实。
- 回退后复查 API/报表版本；首次只有一个成功快照时没有可回退的 previous，应修复后重跑。
- 保留三张中间表、历史事实和业务源记录；不执行 DROP、源库 UPDATE、发布任务重跑。

## 注意事项

本地测试不替代真实部署。任何凭据只留服务器配置；备份目录应按安装器 0700 权限保留。当前与上一成功查询版本保留，旧历史日期继续通过逐日版本清单引用；未引用暂存批次有限清理，失败不撤回已发布结果。
