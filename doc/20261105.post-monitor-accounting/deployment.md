# 生产验收

- 部署提交：`82dcc3528df7ad0bbe889660886081650d87ec7e`，已先推送 GitHub，服务器 fetch 校验精确提交后执行。
- 时间：2026-11-05 11:00–11:03 北京时间。
- 备份及运行副本：`/mnt/data-disk/post-monitor-accounting-20261105/deployment-20261105T030044Z`。
- 原调度运行目录：`/mnt/data-disk/pipeline-dispatch/youtube-task-isolation-20261105T024833Z/coordinator`。以其完整代码为增量基础，保留同时进行的 YouTube 维护；仅覆盖报表包、views.py、queue_states.py。
- 原报表运行目录：`/mnt/data-disk/post-repair-20261016/releases/report-e2da31a69e23a333ba9e92b970a99476ec5d9d68`。

## 数据验证

生产只读沙箱运行 `post_daily_report.py --preview --date 2026-11-04` 成功；独立预览目录未使用正式发送账本，未调用飞书发送。卡片 25175 字节，低于已有安全上限。

| 渠道 | 应发 | 当日计划成功 | 次日补发 | 未完成 | 往期昨日完成 |
| --- | ---: | ---: | ---: | ---: | ---: |
| TT | 64 | 26 | 0 | 38 | 0 |
| FB | 295 | 97 | 0 | 198 | 8 |
| X | 602 | 175 | 0 | 427 | 318 |

FB 当日实际确认数为 97+8=105。原始正式归档 report/card/receipt 三个 SHA-256 均与审计前一致。

监测默认今天筛选仍能查到前一天的 `canonical_task_not_claimable`，状态显示受阻。跨日期积压包含未入队冻结计划；311 条已取消 X 计划单列。X 某旧模板缺失启停历史，全部历史未入队总数保留未知，展示已核实部分和具体说明，不伪造为零。

## 在途保护与回读

仅排空 coordinator HTTP 控制请求，观察从 5 线程/3 连接降至 2 线程/0 连接/0 accept queue，再切换。控制请求保护规则持续 1.455 秒并已删除。全部其他运行中的相关服务 MainPID 前后不变，包括 CPU node、FB/TT/X 原生执行服务、主 API 及并行 YouTube 恢复/隔离服务。无队列重置、占用释放、重新发布或发布配置变更。

新报表只读 SQLite 使用 mode=ro、query_only；补充报表服务读取 dispatch WAL 辅助文件所需目录，主 SQLite 文件保持只读。定时器仍 active。

公网 HTML/JS/CSS 与部署文件 SHA-256 完全一致。按实际页面并发请求 summary/workers/tasks，均返回 200；耗时 4.215/0.052/3.934 秒，低于现有 8 秒网关限制。浏览器验收包含未完成与零待执行并存、统一状态、未知数值、转义和移动布局。

## 回滚

```sh
sh /mnt/data-disk/post-monitor-accounting-20261105/deployment-20261105T030044Z/rollback.sh
```

回滚使用同样的控制请求排空方式，只恢复运行符号链接、静态文件及此项新增 systemd drop-in；不回滚 SQLite、发布账本、回执、租约或业务配置。检测到后续发布修改时停止，保留后续版本。无需重启制作/发布 worker。

原始验证 JSON、HTTP 排空证据、服务 PID 对照、静态文件备份及回滚脚本均保留于上述目录；只读预览及线上回读存于 `/mnt/data-disk/post-monitor-accounting-20261105/verification`。
