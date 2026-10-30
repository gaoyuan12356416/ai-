# 分类补丁部署与独立审查

## 文件和来源

脚本 scripts/deploy_video_editing_material_category.py 复用已有部署工具的原子写、Git 原始字节、磁盘核验、SQLite backup 和服务状态工具；不修改原部署脚本。

严格线上基线 4cff9d3a10af861d4d42184a5deee2f57d4a1461；主代理已只读核实 4 个源码文件及两份静态副本均匹配。唯一 7 个目标：
- /root/drama_material_service/features/video_editing/material_replication.py
- /root/drama_material_service/static/video-editing.html
- /root/drama_material_service/static/video-editing.js
- /root/drama_material_service/static/video-editing.css
- /usr/share/nginx/html/video-editing.html
- /usr/share/nginx/html/video-editing.js
- /usr/share/nginx/html/video-editing.css

不改 routes/service/store、配置、token、unit、GPU 或其他业务文件。线上目录不是 Git 仓库，必须从 CPU 独立干净 checkout 读取已推送确切 SHA。无漂移覆盖参数，发现复合线上文件不同就拒绝。

## 执行

分支 codex/video-editing-material-category-20261030，CPU checkout 为 /mnt/data-disk/video-editing/releases/<SHA>。本地测试并推送 GitHub后，CPU fetch/checkout 确切 40 位 SHA，在该目录执行：

```bash
python3 -B scripts/deploy_video_editing_material_category.py --commit <SHA>
python3 -B scripts/deploy_video_editing_material_category.py --apply --commit <SHA>
```

默认只读。apply 验证挂载 UUID/空间，备份到数据盘 backups/material-category-<time>-<sha>；SQLite backup API 提供一致副本，不执行任何迁移。保存文件 before/after SHA、原模式、API/复刻 worker 状态及旧 worker PID。

临时关闭独立复刻 marker；使用短 BEGIN IMMEDIATE 屏障等待已通过开关的事务提交出站标记，然后等待 submitting=0（默认90秒，可用 --drain-timeout 1..180）。排空超时不停止 worker、不改 live 文件，并恢复原 gate。只有排空后才停复刻 worker、替换 7 文件、重启主 API/复刻 worker、读回并恢复原 gate。预先关闭的 gate 内容保持不动，不擅自开放。

旧 video-editing-worker.service 与 video-editing-material-source.service 的 ActiveState/MainPID 必须保持。原 API PID 可能受其他发布变化，脚本以本次真实快照为准，不使用历史 PID 强制覆盖。

## 回滚

```bash
python3 -B /mnt/data-disk/video-editing/releases/<SHA>/scripts/deploy_video_editing_material_category.py --rollback <backup-directory>/manifest.json
```

只接受分类补丁的7文件 manifest。先校验文件和备份 SHA，快照当前 gate/服务，短暂关闭并排空，再恢复分类补丁旧文件与当前服务状态；保留回滚开始时的 gate 状态（包括后来人工暂停）。SQLite 只额外备份，绝不还原，所有 receipt/category 数据保留。坏备份在任何服务变更前拒绝；中途恢复失败明确标记需人工处理。

## 独立审查

reserved_ui_review 读审其他代理的 backend/frontend 增量：精确7值验证；intent 和 canonical_request 仅显式字段入摘要；B键代码不变；DTO缺失为null；新表单混剪、分类独立、原请求重放未注入默认；分类冲突明确展示且不新排队。未发现剩余阻断问题。

主代理独立审查本部署 helper 已确认：7目标、来源SHA、无旧worker停止、marker+事务屏障、账本保留和复刻worker恢复流程符合范围。12项部署测试实际通过。生产实际发布证据由主代理追加。
