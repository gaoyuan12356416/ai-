# 部署与回滚

## 已部署版本

- 时间：2026-09-15 18:38（北京时间）。
- 主机：43.166.187.96。
- 分支：`codex/meta-video-account-delete-20260915`，已推送 GitHub。
- 运行提交：`d4953ad479b2d8259dee5a9c43662918e46537a3`。
- 精确检出目录：`/mnt/data-disk/meta-ad-asset-delete/release-d4953ad479b2`。
- 运行路径：`/root/drama_material_service`，保留其到数据盘的符号链接。
- 公共静态目录：`/usr/share/nginx/html`。
- 仅切换主 API，PID 3017191 → 3083389；原有 9 个发布定时器全部恢复 active。
- 页面资源版本：`20260915-meta-video-account-delete`；共享导航继续使用 `20260915retired`。

运行代码来自已推送的精确 GitHub 提交。服务器 Python 3.9.6 的 277 项模块测试通过后，按下述流程部署。后续验收文档提交不改变该运行提交。

## 备份与切换

基线为 `b0d5790d3b5b637b27ff22cc53785cc4df82ea37`。5 个 Feature 模块及双份 HTML/JS/CSS，共 11 个部署目标；未覆盖其他模块。

正式代码和台账备份：

`/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T103803Z-d4953ad479b2`

部署前另一个批量预览 `4ca3086cd661468a9eb483d376411e62` 正在执行。首次 prepare 因 `active asset job` 拒绝，未修改代码或台账。操作人明确选择“保留输入参数，部署后重新预览”后执行：

1. 在线备份未中断前的 SQLite，并保存原预览类型、8 个资源 ID、4 个产品及原任务所有者。
2. 验证数据盘 UUID、可写空间和运行路径；确认外部剧集 worker 启用且 active。
3. 暂停原 active 的 9 个发布 timer，等对应 service 自然空闲；确认没有删除 run 或重新核验正在执行。
4. 停止主 API，确认原进程已退出，使用旧 Store 将该唯一预览标记为 `preview_interrupted_for_deploy`。不修改输入、旧删除结果或对象。
5. 用本 release 的 `deploy_meta_video_account_delete.py prepare` 备份 11 个文件与在线台账，再 apply。
6. 用 `safe_restart_drama_api.sh` 启动主 API，并在 finally 中恢复原 active timer。
7. 完成静态哈希、公网接口、原任务所有者会话及台账对比，再重新提交原预览。

预览中断前的证据目录：

`/mnt/data-disk/meta-ad-asset-delete/preview-drain-20260915T103802Z-d4953ad479b2`

目录包含 `tasks-before-preview-interrupt.sqlite3`、`preview-resume.json`、前后服务状态。正式备份另含 `plan.json`、`hashes.json`、原始代码和兼容回滚代码。台账备份仅供取证，不得覆盖当前进度。

## 线上验收

- 11 个运行文件与 GitHub release 哈希一致；公网 HTML/JS/CSS 均为 200，字节哈希一致。
- 共享 topbar 200；未登录访问产品、任务接口均为 401。
- 主 API active/running，启动日志未发现 Traceback、导入或语法错误。
- 原所有者会话读取失败任务 `cdb825c5f40f4c86b6fc66c3dffd6d93` 返回 200：17 个失败 Video 保留，33 个旧成功对象及 1 个 pending Creative 保留。
- 与正式备份相比，14 个任务、171 个对象、80 个尝试、30 个回执及全部其他旧行完全相等；新增账户进度和尝试表均为空，完整性和外键检查通过。
- 本次上线和凭证核验未发出 Meta DELETE。GET-only Probe 的 17 个目标均选中源投放 User631；1 次 GET `/me` 验证身份。远端删除结果由操作人在页面确认后产生。

正式备份中的验收文件：`public-verification.json`、`authenticated-verification.json`、`schema-verification.json`、`service-state-before.json`、`service-state-after.json`。

## 已重新提交的预览

通过原任务所有者会话，仅提交一次普通 `/api/fb-post-ad-delete/preview`，返回 202：

- 新任务：`d2d289fb992f4372bcff7757b4ffb382`。
- 预览：`68d299493c604844ad9fa6afe4015005`。
- 输入类型：`series_code`。
- 资源：XEY271、XEY251、XEY286、XEY246、XEY50123、XEY325、XEY267、XEY317。
- 产品：3442、3360、3443、3543。
- 提交后核对所有者、类型、资源、产品均与原任务相同，且没有执行 run。

`preview-resubmission.json` 保存请求和结果，不含会话 Token。该请求只重新查询预览，未执行任何删除。

## 回滚

先排空删除任务、重核和预览；原 active 发布 timer 暂停后，等待对应 service 自然空闲。若需要中断另一个预览，先保存参数并取得操作人的处理选择，不强制改写删除中的任务。

```bash
python3 /mnt/data-disk/meta-ad-asset-delete/release-d4953ad479b2/scripts/deploy_meta_video_account_delete.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T103803Z-d4953ad479b2
bash /mnt/data-disk/meta-ad-asset-delete/release-d4953ad479b2/scripts/safe_restart_drama_api.sh
```

恢复原 active 的 9 个 timer：x-auto-post-runner、x-auto-post-scheduler、x-post-schedule-claim、x-post-schedule、x-post-manual、tt-post-runner、tt-auto-post-runner、tt-auto-post-scheduler、fb-auto-post-runner。再检查服务、公网及文件哈希。

保留当前 SQLite、新表、账户成功回执和 unknown 锁，不恢复备份台账。prepare 生成并校验的 `rollback_video_graph.py` 会暂停旧版本的 Video 执行入口，防止旧版本把账户模式失败对象改用全局 Video 节点重试；Ad/Creative 保持原流程。原始 Graph 代码仍在备份中。

恢复账户删除版本时，对同一备份执行 `apply`，再按相同流程重启和恢复 timer；无需重新 prepare。apply 只接受原基线或本备份记录的 `rollback_after` 精确哈希，其他线上变更仍拒绝。Video 必须恢复本账户版本后才能继续执行。
