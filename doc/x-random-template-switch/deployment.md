# 上线与验收记录（2026-10-19）

状态：CPU、GPU、公开静态文件均已上线。仅 X 的四处开关，历史配置/任务保持关闭。生产界面只做读取和弹窗检查，没有保存生产设置，没有通过验收创建真实 Post。

## 精确版本和目录

| 组件 | GitHub 提交 | 当前运行目录 |
|---|---|---|
| CPU 43.166.187.96：X sidecar、X Auto | `9313f9fd8db577f9e1d4106fcd0998b891af8a60` | `/mnt/data-disk/x-post-automation/releases/9313f9fd8db577f9e1d4106fcd0998b891af8a60-x-random`，入口 `/opt/x-post-automation/current` |
| CPU 主 API | 同上白名单 | `/root/drama_material_service`；只覆盖 app 两处参数透传、共享 store/client 和新增模块 |
| GPU 43.154.250.89：X 媒体修复及随机制作 | `1206a815e8491b619b47dca49471023670771daf` | `/data/x-post-media-repair/releases/1206a815e8491b619b47dca49471023670771daf-x-random`，入口 `/data/x-post-media-repair/current` |
| 公开页面 | CPU 版本 | `/usr/share/nginx/html` 与主应用 `static` 六个文件一致，两个模板 JS 缓存版本 `20261019random1` |

GitHub 分支：`codex/x-random-template-switch-20261019`（`gaoyuan12356416/ai-`）。GPU 功能代码在后续 CPU 提交中未变化。后续提交增加部署保护、界面批次摘要、验收测试和文档。

从实际运行代码建立独立分支，保留线上素材池快照、账号语言、随机模板透明原片层以及主 API 截图功能的已有修改；未用旧工作区覆盖线上。原本的 `D:\codex\ai-drama-material-service` 未改动。

## 验收证据

- 全量 X 测试 976 项通过，2 项环境条件跳过；随后新增 Premium 源 Post/目标 Repost 验收，相关 77 项通过。Python 3.9 的 CPU 新模块 14 项、GPU 运行环境 34 项通过。
- Playwright 隔离界面完成默认、保存、回显、配置独立性和手动每批关闭验证。真实已登录浏览器确认：素材池/短剧池保存状态关闭，手动弹窗关闭，三条自动模板列表关闭，既有模板编辑页关闭。未点击生产保存或发布确认。
- 使用真实 GPU HTTP 接口重复请求同一短剧制作，回执逐字段完全相同；同任务修改源 SHA256 返回 HTTP 409 `x_random_job_conflict`，没有重新随机或调用 X。
- 三个真实 GPU 样片均经过 CPU 下载、冻结、GPU 制作、最终 SHA256/大小/时长校验和模拟 X 上传/Post，重复准备禁止再调用 GPU；真实 X Post 数为 0。

| 样片 | 输入/输出时长 | 成片字节数 | 音视频检查 | 成片 SHA256 |
|---|---:|---:|---|---|
| 合成运动/音频测试 | 12.5 / 12.5 s | 8,541,086 | 起点均 0，长度均 12.5 s | `54c9adf954577086d4aacc40224cc40b5f2cde576babe0cf29c76892b5b9d136` |
| 素材 7050905 的已修复真实短剧 | 51.78 / 51.78 s | 32,789,070 | 起点均 0，视频/音频长度差 13.3 ms；8 s PCM 对齐偏移 0 ms，相关系数 0.99932 | `1fa5158b933206c3cdef2295873d6b9059408b03fe17ea437a7ae0576264a702` |
| 会员长视频测试 | 150 / 150 s | 111,120,099 | 起点均 0，长度均 150 s | `96921505ff9326332333c90713c8a92bcc4af627f4ab3c7a019ce7bfa3df3e12` |

真实短剧源片/成片同一时刻截图已人工对比：边框、角落/透明动态元素和轻微画面变换生效，字幕仍清晰。完整验收在 CPU `/mnt/data-disk/x-post-automation/validation/x-random-20261019-{short-v2,drama,long-v2}/result.json`，全部使用独立数据库和模拟 X HTTP 客户端。

生产迁移在副本重复运行后再切换。切换前后队列 4019、发布日志 3996、定时批次 333、手动批次 438；逐表原有列的行内容 SHA256 一致，完整性检查通过，新增开关全部 0，没有改写历史任务。只读线上检查再次确认两个池的版本仍是 material=30、drama=26。

`x-post-automation`、`x-auto-post-service`、主 API、GPU worker 均 active；8810/18833/18820 健康接口 HTTP 200。6 个原活动定时器（manual、schedule、schedule-claim、auto runner/scheduler/metric）全部恢复。六个公开静态文件经 HTTPS 下载逐字节核对。CPU 和 GPU 备份的 `SHA256SUMS` 自检通过。

部署时遵守发布锁：首次遇到原自然任务持锁，未中断任务，恢复定时器后调整为最多 60 秒等待，再成功切换。GPU worker 的依赖隧道会随 stop 停止，已恢复 `x-post-media-repair-tunnel.service`，且部署/回滚脚本同时管理该隧道。FB、TT GPU 服务未重启。

## 备份与回滚

CPU 备份：`/mnt/data-disk/x-post-automation/backups/20261019T101459-x-random-9313f9fd8db5`。
GPU 回滚点：`/data/x-post-media-repair/backups/20261019T100516-x-random-1206a815e849`；旧 release `/data/x-post-media-repair/releases/170e3b1325b71a72fcd6de913982ce92bb77fa40` 保留。

在各服务器已经 fetch 最新本分支的 source checkout 执行：

```bash
# CPU：发现开启任务/配置/自动模板版本则先停 X 发布入口和定时器并阻断回退。
cd /mnt/data-disk/x-post-automation/sources/x-random-20261019
/usr/bin/python3.9 scripts/deploy_x_random_template.py --rollback /mnt/data-disk/x-post-automation/backups/20261019T101459-x-random-9313f9fd8db5

# GPU：CPU 已完成回退或明确暂停相关任务后执行；数据库和成片全部保留。
cd /data/x-post-media-repair/sources/x-random-20261019
/data/x-post-media-repair/runtime/python/bin/python scripts/deploy_x_random_template.py --rollback /data/x-post-media-repair/backups/20261019T100516-x-random-1206a815e849
# 原 GPU 部署回执仅列 worker，回滚后显式恢复关联隧道：
systemctl start x-post-media-repair-tunnel.service
```

不恢复旧数据库，不删除制作回执，不重放历史队列。有已开启任务时，按精确任务/配置核查并暂停；保持防重复和未知结果门禁，再决定回退。不要仅把开关强行改 0 来绕过随机制作。
