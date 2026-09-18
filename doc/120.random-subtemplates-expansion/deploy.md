# 部署与回滚记录

2026-09-18 18:22（北京时间）验收通过。全部使用随机模板的 TT、FB、Drama 制作入口已使用扩展默认目录。新建任务生效，旧冻结配方与已完成成片沿用原身份。

## 发布版本

- 素材及Drama兼容代码：GitHub `codex/random-subtemplates-20-20260918`，实际运行提交 `9c4c3cb4eca260d46df8ab23443a32cda667c20c`。
- TT兼容代码：GitHub `codex/tt-random-subtemplates-20260918`，运行提交 `326e16defb8f0b1aec76e8f36d52bb6359275a98`，严格基于原线上 `16ae57a`。
- FB代码保留 `e3bef98`（含最新并发/容量保护），仅切默认素材配置。CPU业务代码保持原版本，仅更新目录配置。
- 新目录SHA：`24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c`；旧目录SHA：`028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`。

## 实际路径与备份

HK `43.154.250.89`：

- `/data/drama-synthesis-gpu/current` → `/data/drama-synthesis-gpu/releases/9c4c3cb4eca260d46df8ab23443a32cda667c20c`
- `/data/tt-post-gpu/random-current` → `/data/tt-post-gpu/releases/326e16defb8f0b1aec76e8f36d52bb6359275a98-catalog`
- TT/FB素材：`/data/random-overlay-gpu/assets/catalog-24d6fad3174f`
- Drama素材：`/data/drama-synthesis-gpu/assets/catalog-24d6fad3174f`，同一SHA的只读硬链接文件，保持原隔离根要求。
- 新增配置：`/etc/random-overlay-subtemplates/{drama,tt,fb}-20260918.env`；三个worker各新增 `99-zz-random-subtemplates.conf`，追加EnvironmentFile，优先级已从真实进程确认。
- 备份：`/data/random-overlay-gpu/backups/20260918-subtemplates`，含原指针、完整unit/drop-in、原env、切换及最终验证JSON。

CPU `43.166.187.96`：

- 元数据：`/mnt/data-disk/drama-synthesis-catalog/catalog-24d6fad3174f/manifest.json`
- `/etc/drama-synthesis/cpu.env` 仅改变MANIFEST_FILE和MANIFEST_SHA256两项。
- API运行目录 `/root/drama_material_service`，解析到 `/mnt/data-disk/root-storage-20260915/rootfs/root/drama_material_service`。
- 备份：`/mnt/data-disk/random-overlay-gpu/backups/20260918-subtemplates`，含原env、timer状态、精确PID暂停/恢复身份和最终验证JSON。

## 切换与验收

先GitHub push，再HK fetch精确提交。既有共享镜像存在部分旧TT对象缺失，因此TT使用从GitHub新建的独立浅克隆；旧工作树及服务代码未修改。目录manifest使用Git -text，防止Windows换行转换破坏SHA。

逐项验证20个新素材和保留的20条原manifest记录。两套RGBA缓存均38项，总量约29.33 GiB/套，保留旧项；Drama v2离线构建沿用实际native PYTHONPATH。以真实Drama UID校验新目录可读；实际runtime预检38项、CUDA和完整app导入全部通过。

TT入口及七个触发器通过原维护控制器暂停；FB保存并停止prepare.timer。正在运行的FB客户端只对已核对cmdline/PID/start_ticks的主进程SIGSTOP暂停继续领取，GPU与CPU后端仍完成已受理任务。确认三个GPU cgroup无制作子进程、HTTP连接排空及Drama无queued/running后，窄停止并切换worker。无强杀FFmpeg、无数据库迁移或历史成片重做。

三个worker及三个隧道已active、NRestarts=0；GPU8787/8830/8836、CPU18788/18830/18836健康200。CPU主API与job worker读取四组8/10/8/12；Drama真实HTTP目录同样返回新SHA和新数量，公共topbar200。精确SIGCONT恢复FB客户端、恢复TT原七个trigger状态和FB prepare.timer；维护journal restored=true，gate为空。

## 回滚默认目录

保留当前兼容代码及两个SHA的映射。若已生成新SHA冻结任务，禁止直接切回不认识新目录的旧代码或恢复旧数据库。回滚只切默认池：

1. CPU执行原维护控制器的 `gate-on tt --apply`、`pause tt --apply`，保存并停止 `fb-auto-post-prepare.timer`；暂停继续领取并等待已受理任务和HTTP请求排空。控制器绝对路径为 `/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py`。以新的PID/start_ticks重新确认，不能重用本次暂停PID。
2. HK在三个 `/etc/random-overlay-subtemplates/*-20260918.env` 中仅改以下默认值，保留 `RANDOM_OVERLAY_ASSET_CATALOGS` 两项和当前 `DRAMA_GPU_RELEASE_SHA`：

| 文件 | ROOT变量的旧值 | MANIFEST_SHA256变量的旧值 |
|---|---|---|
| drama-20260918.env | /data/drama-synthesis-gpu/assets/fb-v3-028326ab2114 | 028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f |
| tt-20260918.env | /data/tt-post-publisher/random-overlay-assets/v1 | 028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f |
| fb-20260918.env | /var/lib/fb-page-random-overlay/assets/v1 | 028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f |

3. HK窄重启这三个worker及对应三个隧道：`drama-synthesis-gpu-worker.service`、`tt-gpu-publisher.service`、`fb-page-random-overlay-gpu.service`、`drama-synthesis-gpu-tunnel.service`、`tt-gpu-reverse-tunnel.service`、`fb-page-random-overlay-tunnel.service`。验证健康和旧默认SHA。
4. CPU在 `/etc/drama-synthesis/cpu.env` 将MANIFEST_FILE改回 `/mnt/data-disk/drama-synthesis-catalog/fb-v3-028326ab2114/manifest.json`、SHA改回上表旧值，窄重启 `drama-material-api.service` 与 `drama-material-job-worker.service`。
5. 验证GPU/CPU健康、3/5/3/7旧目录数量及新旧冻结配方兼容，恢复原触发器和继续领取，执行控制器 `resume tt --apply`、`gate-off tt --apply`。不删除新旧素材、缓存、检查点、下载、结果或账本。

脱敏证据在同目录 `evidence/`；精确服务状态见cpu-verification.json、gpu-verification.json。

维护技能ai-backend-maintenance上下文已备份更新：random-subtemplates-20-20260918。未修改memory文件。
