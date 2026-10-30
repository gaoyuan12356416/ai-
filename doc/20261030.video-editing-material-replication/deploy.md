# CPU 发布与回滚

脚本：`scripts/deploy_video_editing_material_replication.py`。本脚本只在 CPU 执行；不访问 GPU，不停止原渲染和原素材入库 worker。默认只读预检。

## 来源和文件白名单

要求本地验证后提交推送到 GitHub 分支 `codex/video-editing-material-replication-20261030`。CPU 在独立 `/mnt/data-disk/video-editing/releases/<SHA>` checkout 确切已推送提交；运行脚本会验证 origin、干净工作区、HEAD、远端分支 SHA 与基线祖先关系。

安装到 `/root/drama_material_service`：
- features/video_editing/{routes,service,store,material_replication}.py
- scripts/video_editing_material_replication_worker.py
- scripts/import_video_editing_material_replication_history.py
- static/video-editing.{html,js,css}

静态三文件同时安装到 `/usr/share/nginx/html`；新 unit 安装到 `/etc/systemd/system/video-editing-material-replication.service`。仅更新共同环境文件中两个非敏感配置：

```ini
VIDEO_EDITING_MATERIAL_REPLICATION_ENABLED=1
VIDEO_EDITING_MATERIAL_REPLICATION_DISABLED_FILE=/etc/video-editing/material-replication.disabled
```

源码未配置时默认关闭。部署先创建独立 marker，存在时禁止新入队及 worker 领取 pending；历史查询和已有 operation_id 的只读复用仍可用。开放仅移除该 marker，不需额外重启。

## 私密配置

`/etc/drama-material-service/a-group-expansion.env` 已独立保存 A_GROUP_TASK_CREATE_BASE_URL、A_GROUP_TASK_CREATE_TOKEN，必须 root:root、0600、非软链接。仅新 worker 的 EnvironmentFile 引用它，主 API 不引用。脚本只 stat 此文件，不读取或备份 token 内容。B 组真实鉴权为自定义 token 请求头，不是 Authorization/Bearer。

## 预检与安装

CPU 先从 GitHub fetch 并 checkout 审核过的完整 SHA。以下命令在该 checkout 执行，SHA 必须替换为实际 40 位值：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/deploy_video_editing_material_replication.py --commit <SHA>
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/deploy_video_editing_material_replication.py --apply --commit <SHA>
```

默认以 git show 基线原始字节校验旧文件，候选也直接取 git show，避免 Windows CRLF 影响。若有已审核的生产叠加修改，可传 `--expected-before <JSON>`，JSON 仅允许白名单绝对路径到旧 SHA256/null 的映射；不允许任意路径。

安装前核验数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、可用空间及 DB，拒绝链接备份目录。备份位于 `/mnt/data-disk/video-editing/backups/material-replication-<timestamp>-<sha>`，manifest 保存文件 before/after SHA、原模式、原主 API 状态和受保护 worker PID。

使用 SQLite backup API 生成一致副本，复制副本执行 Store 增量迁移，逐表核验旧数据不变且新账本存在，再迁移 live DB。安装只重启主 API并启动新 worker，校验原 `video-editing-worker.service`、`video-editing-material-source.service` 的 MainPID/ActiveState 未变。成功状态为 installed_disabled。

## 历史导入与关闭期间验证

由发布负责人从已冻结审计目录执行导入 CLI，先不加 --apply 核对，只在证据匹配后应用。使用主 API 运行所需配置，不加载 B 组私密 env。

```bash
python3 -B scripts/import_video_editing_material_replication_history.py --task-id <ve_id> --audit-dir <rejected-audit-dir> --audit-dir <accepted-audit-dir> --expected-resource-id 28331 --default-operator-override susiqi
python3 -B scripts/import_video_editing_material_replication_history.py --task-id <ve_id> --audit-dir <rejected-audit-dir> --audit-dir <accepted-audit-dir> --expected-resource-id 28331 --default-operator-override susiqi --apply
```

导入不得创建 pending，不得调用外部接口；重复导入应返回原记录。默认归属纠正只存在新 metadata。真实样例任务 1732014 的导入、已有回执复用及参数冲突展示需留证，不重新生产。

关闭 marker 时新 operation 的 POST 仍被禁止；如需验证相同 B 键复用，只在独立进程内用受控 service helper 临时覆盖 enabled 判断，保持磁盘 marker、worker 和外部 HTTP 隔离，并核对账本未新增 pending。不能为了测试而提前开放入口。

## 开放与实际验收

确认导入/复用、权限、接口链路和静态文件读回后：

```bash
python3 -B scripts/deploy_video_editing_material_replication.py --enable <backup-directory>/manifest.json
```

该操作重新验证文件哈希、worker 状态、API 健康和旧 worker PID，再移除本次 marker；丢回执可按同 manifest 核对。不得把公共 401 或本地 fixture 浏览器结果写成已登录生产浏览器验收。生产通过实际 actor 的进程内 API 链路、公共未登录 401、公开静态文件验证分别记录。

## 回滚

```bash
python3 -B /mnt/data-disk/video-editing/releases/<SHA>/scripts/deploy_video_editing_material_replication.py --rollback <backup-directory>/manifest.json
```

回滚先验证候选/备份 SHA，关闭新入口、停止/禁用新 worker，再恢复本次精确文件，恢复主 API 原状态并核对旧 worker PID。新增文件只删除本脚本允许且哈希一致的目标。SQLite 绝不还原备份或删新表，已导入/已提交/未知记录全部保留；marker 保持关闭。文件已漂移或备份损坏时拒绝覆盖，留待人工核对。

安装失败也保持关闭，并尝试仅恢复已写入文件；即使 unit 尚未写入，也须恢复前面文件。manifest 的 failed_manual_recovery_required 状态必须人工处理，不能声称自动回滚成功。
