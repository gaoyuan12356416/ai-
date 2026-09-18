# 生产验收与运行证据

日期：2026-09-18，北京时间。范围：池 62「dramawave做排重实验」、模板 1/version 3、运行 119..124、145 个唯一 Page。此文由主任务记录实际操作，QA 独立结果见 test-report.md。

## 已上线版本

| 提交 | 变更 | 状态 |
| --- | --- | --- |
| e3bef98 | 制作/发布持续领取、GPU 双路与资源保护 | CPU/GPU 已部署，固定输入成片哈希一致 |
| 4201910 | 按 Page 语言选材、移除模板语言、未来运行安全扩充 | CPU 与两份静态目录已部署 |
| 284f06e | 发布 8 路、30 分钟领取窗口、提前两天制作 | CPU 已部署 |
| f7cb58f | 仅自有 COS 历史 HTTP 同对象升级 HTTPS | CPU 已部署，TL 取得 500 合格候选 |
| 1f64840 | GPU CPUShares 4096，保留 4 核/8 GiB/2 jobs 上限 | GPU 已部署 |
| 90faff9 | 7 语言最多 3 路查询、匹配规划预算与租约 | CPU 已部署，192 项 FB 全套通过，23.191 秒 |
| 20eb3a8 | 有限日期的服务器只读巡检 | CPU 已部署，17 项新增独立测试通过 |

所有版本先提交并推送 GitHub 后部署。仓库分支 `codex/fb-145-capacity-20260918`，仓库 `gaoyuan12356416/ai-`。

CPU 运行目录为 `/opt/fb-auto-post/current`，真实路径为 `/mnt/data-disk/root-storage-20260915/rootfs/opt/fb-auto-post/releases/d2a6e91f83ec34f188f41c5d8abb413b0bc1d2b5`。该目录不是 Git checkout，本次只同步经审核的精确文件，保留线上既有到期目标审计修复。

GPU `/opt/fb-page-random-overlay/current` 指向 `/data/random-overlay-gpu/releases/e3bef98`。GPU 源码 checkout `/data/random-overlay-gpu/capacity-source-20260918`，资源 drop-in 为 `/etc/systemd/system/fb-page-random-overlay-gpu.service.d/90-capacity.conf`。

CPU 15 个应用、静态资源和业务 unit 文件在 17:11 验证 SHA256 与 `90faff9` 一致；两份静态目录 `/usr/share/nginx/html` 和 `/root/drama_material_service/static` 同步。GPU 三个工作文件及资源配置记录独立哈希。巡检三个文件在 17:35 再次验证与 `20eb3a8` 一致。

## 排程扩充与历史保护

17:19:48 完成读回，运行 119..124 每个均有 145 条任务、145 个唯一 Page，新增 `6 × 122 = 732`。原有 1511 条任务、2245 条尝试、1205 条账本与在线备份逐行一致。扩充过程持续暂停五个业务 timer，且无 preparing/running/submitted/unknown；读回完成后才恢复。

- 操作 ID：`fb145-20260918-page-languages`。
- 副本预演及正式 apply 指纹：`4169bb269e823e6e905bb3334d17610c553d66753260781aff2fcc529d08efa0`。
- 冻结候选 SHA256：`f6ee45c6e294eae76a493ff06e83dd753ae36e23b91d05dde4afc71801c12b1c`。
- 7 种语言各 500 条合格候选，JSON 与 metadata 权限 0440。所有 732 条新增任务的语言、素材和剧均与 Page 语言一致。
- 原 23 个 Page 今天保持 18:54；新增 122 个 Page 今天为 21:30。
- 明天 02:48、05:10、10:05、17:40、21:39 各 145 个 Page，共 725 条。

历史 12 条 skipped 保持原状态，未重放过去的时隙。145 个 Page 的只读 Graph 授权检查均通过，但这不是后续实际发布成功的替代凭证。

## 当前实际进度

服务器报告时间为 17:35:46：未来 870 条任务中 planned=717、preparing=2、ready=151，其中新增任务 ready=13。五个业务 timer 全部 active，CPU 18835 与 GPU 隧道 18836 的 `/health` 均通过；巡检 `healthy`、issues 为空。

今天截至快照有真实 published 记录的 Page 为 20，明天为 0（未到发布时间）。今日此前已发布 80 条。不能把 ready、submitted、服务健康或完整排程称作全部发布成功。

自然制作任务 1512、1513、1514 的公网成片通过 TLS HEAD：HTTP 200、video/mp4、Content-Length 与数据库相等、`x-cos-meta-sha256` 与 `x-cos-meta-profile` 均匹配。未为了验收创建 Graph 测试 Post。

## 容量与限制

345 个历史制作样本平均 114.24058 秒，中位 93 秒，P95 307 秒。相同两条输入的顺序制作 140.333 秒、双路 120.069 秒，整体加速 1.169 倍；四份结果与原参考成片的哈希一致。

按明天实际所选片长加权，725 条约需 20.19894 小时，约余 3.8 小时/日，单机可承担但不能视为充足冗余。今天新增 122 条平均输入时长 307.04098 秒，模型约需 3.89216 小时；17:19:48 启动至 21:30 约有 17 分钟模型余量。网络、片长与其它作业波动可能压缩余量，巡检按跨时隙累计剩余时长检查风险。

制作服务资源上限为 4 核、8 GiB、2 jobs，CPUShares 4096；缓存 96 GiB，32 GiB 空间保留。GPU `/data` 属于根文件系统，不是单独挂载数据盘；本次未在活跃任务期间迁移。CPU 数据与日志在已挂载 `/mnt/data-disk`，巡检同时验证挂载和可用空间。

## 替代 Codex 自动任务

用户要求关闭的 heartbeat `dramawave-145-page` 已设 `PAUSED`，通过 view 及本地 automation.toml 二次确认。没有另建 Codex 定时任务。

`fb-page-145-readiness.timer` 已 enabled/active，9 月 18/19 日每 5 分钟执行只读检查，9 月 20 日 00:30:45 收尾，显式 `Asia/Shanghai`。之后没有配置新的日期触发。systemd 239 日历与 unit 验证通过；17:35 读回 service Result=success、ExecMainStatus=0、CPUQuotaPerSecUSec=200ms、MemoryMax=268435456。

脚本以 SQLite mode=ro + query_only=ON 读取，检查范围、语言、失败/跳过/未知、制作截止余量、过期未领取、超过两小时平台未确认、服务与磁盘；不发布、重试、修改账本或发送外部通知。业务服务仍按原自动调度执行。

生产报告：

- `/mnt/data-disk/fb-auto-post-publisher/health/145-page/latest.json`
- `/mnt/data-disk/fb-auto-post-publisher/health/145-page/observations.jsonl`，4 MiB 轮换保留 previous。
- scope.json 冻结 145 Page 语言和 732 个新增任务的输入时长，权限 0600。

## 审计与备份

CPU 根目录：`/mnt/data-disk/codex/tasks/fb-145-capacity-20260918`。

| 路径 | 内容 |
| --- | --- |
| backup-capacity/ | 初始配置、unit、runner、SQLite 在线备份 |
| backup-capacity/env-before-lookahead | 调整提前制作前的环境配置 |
| backup-capacity/final-window/ | 调整发布窗口前的配置与 unit |
| backup-language/ | 原语言代码、两份静态资源、SQLite 在线备份 |
| backup-language/repositories.before-https-fix.py | HTTPS 修复前仓库实现 |
| backup-language/bounded-query/ | 限制语言查询并发前的相关文件 |
| before-extension-apply.sqlite3 | 正式追加前在线 SQLite 备份 |
| extension-rehearsal-*.sqlite3 | 副本预演数据库 |
| language-candidates.json、language-candidates-metadata.json | 冻结候选与元数据 |
| extension-applied.json、queue-validation.json | 追加回执与历史逐行不变验证 |
| final-production-verification.json | 自然成片 HEAD、巡检文件哈希、unit 读回 |

GPU 备份目录 `/data/random-overlay-gpu/backups/fb-capacity-20260918`，含 previous-release.txt、dropins/、capacity-before-share.conf、deployed-manifest.json，旧 release 984d663 保留。

本地脱敏证据目录：`C:\Users\gaoyu\Documents\New project\output\fb-145-capacity-20260918`。用户报告为 report.md，最新服务器快照为 server-readiness-snapshot.json。

## 回滚与验证边界

代码/配置回滚前先停相应触发并等待制作、发布和核对作业排空，避免中断正在上传或提交的平台请求。CPU 按精确文件备份恢复，GPU 按 previous-release 和 drop-in 备份恢复；GPU 重启后核对反向隧道与 CPU 18836 健康，再恢复原业务 timer。当前存在 145 Page 排程，回退较小的旧容量前必须另行评估其截止时间。

独立撤销本次巡检可 stop/disable `fb-page-145-readiness.timer`，不影响五个业务 timer。不要重新启用已由用户关闭的 Codex heartbeat。

禁止用旧 SQLite 覆盖现有任务、尝试和发布账本；不得借回滚重放历史任务或未知结果。未来运行扩充 CLI 依赖完整维护窗口，不能在当前活跃生产链路再次执行。

登录后的线上模板保存未操作；输入/兼容逻辑已有自动测试，正式页面无语言控件已通过未登录 DOM 验证。实际平台完成状态将在到期后写入正常账本和服务器检查报告，本文不提前认定成功。
