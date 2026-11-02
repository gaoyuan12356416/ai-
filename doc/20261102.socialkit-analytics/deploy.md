# 部署文档

## 变更内容

独立 SocialKit 报表、授权模块 `socialkit_analytics`、本地查询快照、每日采集服务和查询技能。当前状态：本地开发/47 个独立测试通过，尚未记录生产部署成功；部署提交、备份目录、回填版本及启用时间待实际执行补入。

线上共享 app.py 与 Git 基线可能不同，安装器在当前线上文本中应用必要锚点补丁，并先生成备份与哈希清单；不整份覆盖共享源码。

## 配置项

- 页面：`https://ai.yingliangads.com/socialkit-analytics.html`；API 前缀 `/api/socialkit-analytics/`。
- 应用：`/root/drama_material_service`；静态文件同时安装至该目录 static 和 `/usr/share/nginx/html`。
- 发布指针：`/opt/socialkit-analytics/current`；数据根：`/mnt/data-disk/socialkit-analytics`。
- 私有环境：`/etc/socialkit-analytics.env`，权限 0600，复用现有 source/reader/writer 配置；文档和 Git 不保存值。由 systemd EnvironmentFile 解析，不用 shell 展开或直接打印文件。
- 数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。刷新独立服务 `socialkit-analytics-refresh.service`、timer 同名；11:00 Asia/Shanghai，Persistent=true，单实例锁。

## 数据库变更

仅在 `101.32.56.53:63353/ads_ai` 创建 `socialkit_effect_daily`、`socialkit_publish_daily`、`socialkit_refresh_runs`。金额整数分，版本+行键主键；运行刷新无 DDL。内部源读 63350 并校验 read_only=1，源任务/短链连接使用只读事务；不改业务发布记录或令牌。

## 部署步骤

以下为执行清单，实际结果尚待记录：

1. 将审查代码提交并推送 GitHub，CPU 拉取该精确提交到干净 release；核对 commit、工作区和必要依赖（Python 3.9、PyMySQL、mysql SQL Gate）。
2. 在 release 运行 `python3 ops/socialkit-analytics/install.py --prepare-env`。通过 systemd 临时单元加载 EnvironmentFile 后运行该脚本的 `--migrate`，不得把密码写到命令行。
3. 运行 `python3 ops/socialkit-analytics/install.py --install <commit>`；保存返回的 backup/release/commit。重启 `drama-material-api.service`，核对 health 和新增匿名 API 的 401。
4. 通过 systemd 临时单元加载同一 EnvironmentFile、WorkingDirectory 指向 release，运行 `python3 -m features.socialkit_analytics.refresh --root /mnt/data-disk/socialkit-analytics --start 2026-10-19 --end 2026-11-01`。逐日顺序读取，所有日期/控制量/63350 验证完成后发布。
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

- 代码回滚：从已部署 release 执行 `python3 ops/socialkit-analytics/install.py --rollback <backup>/manifest.json`。先核对当前文件仍匹配部署哈希，全部通过后停 timer、恢复必要文件/旧 release 指针并重启应用。若线上文件有后来修改，拒绝自动覆盖，且不提前停用 timer。
- 查询快照回退：执行同脚本 `--rollback-snapshot`，获取数据根 refresh.lock 后检查 current/previous 数据文件，再交换两份指针；不重新采集、不改中间事实。
- 回退后复查 API/报表版本；首次只有一个成功快照时没有可回退的 previous，应修复后重跑。
- 保留三张中间表、历史事实和业务源记录；不执行 DROP、源库 UPDATE、发布任务重跑。

## 注意事项

本地测试不替代真实部署。任何凭据只留服务器配置；备份目录应按安装器 0700 权限保留。当前与上一成功查询版本保留，旧历史日期继续通过逐日版本清单引用；未引用暂存批次有限清理，失败不撤回已发布结果。
