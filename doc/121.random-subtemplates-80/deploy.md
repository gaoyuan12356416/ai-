# 部署与回滚

2026-09-20 已完成生产部署。HK 验收时间 `2026-09-20T12:29:23+08:00`，CPU 验收时间 `2026-09-20T12:29:26+08:00`，均为北京时间。四组各 20 个，共 80 个活动子模板，160,000 种基础四层组合。

## GitHub 和生产位置

- 分支：`codex/random-subtemplates-80-20260920`，已推送。
- 素材发布提交：`cededc111d10150e9ee378e3d32226e2cd4de6f5`。HK 从 GitHub 获取该精确提交，工作树干净；发布源位于 `/data/random-overlay-gpu/releases/cededc111d10150e9ee378e3d32226e2cd4de6f5-assets80`。
- 已复核的常规运维脚本提交 `cb8f3d3`；存量停滞处理脚本分别在 `b8631e2` 和 `a257465` 推送后执行。实际脚本归档在 `operations/`，结果收据在 `evidence/`。
- 目录 SHA256：`b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c`。
- HK `43.154.250.89`：TT/FB 使用 `/data/random-overlay-gpu/assets/catalog-b5df776a88bd`；Drama 使用隔离目录 `/data/drama-synthesis-gpu/assets/catalog-b5df776a88bd`。
- CPU `43.166.187.96`：元数据 `/mnt/data-disk/drama-synthesis-catalog/catalog-b5df776a88bd/manifest.json`。

## 生效范围及兼容

所有 TT、FB、Drama 随机模板制作入口已使用新默认池。新建、尚未冻结配方的制作使用这 80 项；既有冻结配方与已完成成片保留原版本身份。未迁移业务数据库、未手工重放任务或发送测试帖子。

两个旧目录 `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f` 和 `24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c` 的文件、SHA 和受信映射均保留。light 的 2 条历史记录不参加随机选择。

应用代码指针保持 Drama `9c4c3cb4eca260d46df8ab23443a32cda667c20c`、TT random `326e16defb8f0b1aec76e8f36d52bb6359275a98-catalog`、FB `e3bef98`。TT 原 direct-outro 指针未改动。三个 GPU 通过追加 `/etc/random-overlay-subtemplates/{drama,tt,fb}-20260920.env` 切默认目录；CPU 只修改 `/etc/drama-synthesis/cpu.env` 的 MANIFEST_FILE 和 MANIFEST_SHA256 两项。

## 验收结果

19 项定向回归、42 项新素材逐帧检查、2000 种子覆盖和重复性验证均通过。16 段私有 GPU 样片全部通过，每段 5 秒、720×1280、30fps、150 帧且音频完整；其中 12 段覆盖全部新样式，2 段验证旧目录配方，另有 TT HEVC 和 FB H264 各 1 段。

- `/data/random-overlay-gpu/asset-cache-v1`：80 项，41,295,480,040 字节（38.46 GiB）。
- `/data/drama-synthesis-gpu/assets/rgba-nut-demux-v2`：80 项，41,295,477,461 字节（38.46 GiB）。

三个 GPU worker、两个 CPU 服务及三个反向隧道均已启动并通过健康检查；实际 Drama HTTP 目录与 CPU 两个进程均核实四组各 20。七个 TT 触发器恢复原状态，TT gate 清除，FB prepare.timer 恢复 active，暂停的 FB 客户端按 PID/start_ticks 恢复。公共 `/api/ui/topbar` 返回 200。精确服务状态和目录收据见 `evidence/hk-final-verification.json` 与 `evidence/cpu-final-verification.json`。

切换前发现一条已停滞超过 80 分钟的旧 FB 转码。先保存原视频与现场，正常退出无响应后仅终止该转码；现有错误路径保留源视频并清理未完成输出，无 ready manifest 或人工重发。详见 `bugs/BUG-003.md`。这次扩容没有修改或宣称修复该存量卡顿的根因。

## 备份与回退

- HK：`/data/random-overlay-gpu/backups/20260920-subtemplates80`。
- CPU：`/mnt/data-disk/random-overlay-gpu/backups/20260920-subtemplates80`。
- 存量 FB 停滞现场另存 HK `/data/random-overlay-gpu/backups/20260920-stalled-prepare`。

原始 env 只保存在权限 0700 的服务器备份目录，未提交 Git。回退默认池到 9 月 18 日 `24d6fa…` 的逐步操作见 [rollback.md](rollback.md)。回退必须保留三个 SHA 映射、80 个素材和兼容应用代码；不得恢复旧数据库或重制完成任务。

维护技能上下文已备份后更新为 `random-subtemplates-80-20260920`，旧 38 项目录条目标记 retired，其余条目保持不变。更新收据见 `evidence/skill-context-update.json`。
