# X Post 日志队列管理

页面移除自动批次区域和相关批次请求；明细补充“手动发布”来源及筛选，支持当前页单选、全选、批量重置、单条重置和停止。操作列和勾选列固定，结果逐条显示。原自动调度配置不变。

`GET /api/admin/x-posts/logs` 使用只读本地投影，区分 `manual_publish`、`auto_publish`、`material_pool`、`drama_pool`，附带当前可执行操作和不可执行原因。既有日志字段、权限和分页保留。

`POST /api/admin/x-posts/queue-actions` 仅限 Cookie 管理员、同源 JSON，接受 `operation_id`、`action=reset|stop`、`items=[{queue_id,version}]`，最多 100 条。版本来自列表的 `action_version`；操作编号用于响应丢失后的幂等查询/重试。请求中的身份不可覆盖会话身份。

每条操作在 X 和本机调度 SQLite 的附加数据库事务内执行。停止使用原有 canonical hold 和调度暂停状态，保留原队列、预留、成片及账本；重置只移除此操作创建的暂停，并重新开放原失败或待执行阶段，不新建素材或发布队列。原日志失败、尝试次数、队列和调度代次保存在 `x_post_queue_control_audit`；原调度请求、操作、回执与发布事实不删除。旧代次立即失效，重新领取使用新代次；已完成制作及可复用成片保留。

已发布、已有源 Post/转推回执、结果未知、真实在途执行、有效占用、其他人工保留/归档均逐条跳过。运行中操作不强杀进程、不清租约；等待本次处理结束后再操作。重置不修复账号或平台错误，后续执行继续遵守账号、媒体、语言、素材及防重检查。

## 验证

`python -m unittest scripts.test_x_post_log_management.Controls -v`：10 项涵盖手动来源、停止/恢复、自动模板失败恢复、上传失败成片复用、未知/已发布/在途保护、部分成功、过期选择、响应丢失幂等、占用保留、事务回滚、只读查询及权限边界。

Playwright 本地模拟验证当前页全选、批量重置、来源筛选、单条停止、逐条结果及桌面/手机页面，无真实 X 写入。JavaScript/Python 语法及 `git diff --check` 通过。

扩展原生桥回归中 17 项通过，原有 `test_native_account_fence_blocks_same_account_during_upload` 仍要求账号级互斥，与本任务基线已上线的队列独立执行规则冲突；对应运行代码和旧测试未修改，不恢复该历史门禁。

## 发布与回滚

精确提交先推送 GitHub，再由 CPU `/mnt/data-disk/pipeline-dispatch/github` fetch/archive。部署脚本只发布主 API 入口、新日志模块和页面资产；备份文件、在线 SQLite 快照、源版本、服务 PID、演练与切换记录保存在数据盘。先在数据库副本演练重置与停止/恢复，验证冻结身份、原请求和发布回执不变。

```sh
python3.9 scripts/deploy_x_post_log_controls.py prepare SOURCE COMMIT BACKUP
python3.9 scripts/deploy_x_post_log_controls.py rehearse SOURCE BACKUP
python3.9 scripts/deploy_x_post_log_controls.py activate BACKUP
python3.9 scripts/deploy_x_post_log_controls.py rollback BACKUP
```

切换只短暂阻止主 API 新 HTTP 连接，排空该服务请求并检查已知后台线程正在等待后重启主 API；入口限制有自动解除计时器。X OAuth/发布服务、调度器、CPU/GPU 执行节点和原定时任务保持运行。Nginx/主 API/X 运行目录的静态来源同步。回滚恢复代码和页面，保留当前数据库、操作审计、暂停、回执、凭据和新发生的业务事实；不会自动恢复已停止的发布队列。

## 2026-11-05 已部署

运行代码提交 `269f7ceccd900a0c16f6a149273a1410d343d4d3`，CPU `43.166.187.96:/root/drama_material_service`。备份和证据：`/mnt/data-disk/x-post-log-controls-20261105/deploy-269f7cec`。副本演练完成 12 条失败重置及 12 条停止/恢复，冻结身份和请求/回执历史摘要不变；另通过离线完整“失败后重置→上传→确认回执→禁止再次重置”链路。

公网 HTML/JS 和九个部署文件校验通过；未登录的日志 GET 和操作 POST 均返回 401；真实只读列表每行包含可操作状态，手动来源筛选正确。主 API PID 由 3311101 切换为 3059839；X 服务 3311120 和调度器 1335330 未变，均 active。生产未创建操作审计表，证明本次部署没有执行线上重置/停止。临时入口规则已移除。

```sh
python3.9 /mnt/data-disk/x-post-log-controls-20261105/source-269f7cec/scripts/deploy_x_post_log_controls.py rollback /mnt/data-disk/x-post-log-controls-20261105/deploy-269f7cec
```
