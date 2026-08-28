# 美国 GPU 迁移执行记录

批次：`gpu-service-migration-20260828T1502`。本文件记录已取得的证据，
不是全量迁移完成或人工验收结论。时间未特别标注时为北京时间。

## 当前边界

- 美国 `43.166.178.132`；香港 `43.154.250.89`；CPU `43.166.187.96`。
- Kronos、香港 FB、CPU 主 API 与现行香港剧集进程不重启、不迁移。
- 香港 `/data` 使用现有根卷，执行开始实测已扩至约 504 GiB，约 428 GiB 可用。
  不要求 `/data` 独立挂载；所有新增业务均保留至少 30 GiB 容量余量。
- CPU 数据盘 UUID 为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`；
  不允许缺盘时回退根卷。
- 没有创建真实 TikTok/X 验收帖子，没有手动补跑过期发布时间。

## 已完成：X 修复

切换窗口约为 15:47–15:58。先关闭 CPU X 写入口、暂停原触发器并排空；
在线备份两套 CPU SQLite 后，停止、禁用并持久屏蔽美国 X worker 和 tunnel。
香港 X 切换独立运行环境及状态目录后，再恢复 CPU 原触发器和写入口。

| 项目 | 实际结果 |
| --- | --- |
| 业务代码 | `fba8ff603e979b443339108cb2ce45c975fbd39f`，未修改 |
| 香港目录 | `/data/x-post-media-repair/{releases,runtime,state,config}` |
| CPU 入口 | 保持 `127.0.0.1:18820`，实际由香港 SSH 隧道持有 |
| Profile | `x-h264-nvenc-720-duration-policy-v5`，健康接口返回正常 |
| 香港原记录 | 71 条全部保留原字节 |
| 美国归档 | 411 份正式 manifest 全部归档；92 条通过当前 profile 与产物检查后补入 |
| 冲突/旧记录 | 5 条冲突保留香港；314 条旧 profile 仅归档，不重修、不发布 |
| 最终记录数 | 163 |
| 临时目录 | 从实际进程 mount namespace 验证 `/tmp` 和 `/var/tmp` 均绑定 `/data` |
| 美国状态 | `x-post-media-repair.service` 与 `x-post-media-repair-tunnel.service` 均 inactive、masked |

历史 X 未知结果 `publish_log/queue=726` 保留原行，未重试、未刷新状态。
其行校验 SHA256 为
`0018e4d97449d0f6576f27abfee5bc957f4c867d3299fa434be4ac50987dfb5d`。
切换前后发布队列分布均为 49 failed、677 published。

香港 FB PID `1207342` 和剧集 PID `1188891` 在独立核验时未改变；
FB 仍使用的 `/opt/x-post-media-repair/venv` 保留。

主要证据（路径均以对应主机批次目录为前缀）：

- CPU `control/x-sqlite-before/`：两库在线一致性备份、quick_check、文件 SHA。
- CPU `control/x-coordinator-verification.json`：入口、进程、未知结果保持证明。
- CPU `x-hk-entrypoint-verification.json`：实际 18820 请求与端口归属。
- 美国 `source-fence/x-before.json`：原 unit/启动状态及回退资料。
- 香港 `hk/x-manifest-import.json`：逐条保守导入分类。
- 香港 `hk/x-runtime-file-evidence.json`：真实运行路径、配置权限及临时目录映射。
- 香港 `hk/x-offline-ffmpeg.json`：NVENC、NVDEC 本地合成检查；不代表真实外部发帖。

## 已准备但尚未切换

### TikTok 两条链路

原业务代码固定为 `9425b39fa45390b3dc107f353dc6ef436415365d`。
香港独立 Python 3.10.20、精确依赖、关闭发布闸门的两套 unit 已安装，
服务尚未启动，也未建立生产隧道。资源和状态仅完成预复制，不是最终冻结同步。

73 项源版本 Fake API 测试通过；私有 FFmpeg 的 2 秒 HEVC_NVENC 冒烟通过。
美国新 FFmpeg 需要香港驱动尚不支持的 NVENC API 13.0，因此采用香港现有
兼容版本的私有副本；不升级系统驱动，不改媒体 profile、业务逻辑或已冻结 URL。
完整隔离验证中 random_overlay 渲染与复用通过；direct_outro 初次验证失败，
必须诊断并通过后才可进行生产交接。失败证据保留，未触碰生产账本。

### CPU 截图与封面

三个接管 unit 已安装但 inactive/disabled，原 CPU worker 未重启。
五个公共/任务目录仅预复制，原路径尚未切换。30 份缺失历史图片共
13,126,736 字节已逐文件校验导入数据盘 staging，冲突保留 CPU 原文件。
美国大历史视频留在美国可校验归档中，不覆盖当前香港/COS 结果。

CPU 包 15 项本地测试通过；新增失败尺寸重试测试另在 CPU 对实际运行的
`/root/drama_material_service/app.py` 函数执行，1/1 通过。该测试使用模拟 DB、
队列和生成接口，仅在数据盘创建临时样本，证明只重试失败尺寸、成功 URL、
文件 SHA 和 mtime 保留。真实三尺寸及封面生成尚未执行。

### 香港广告与视觉

源代码、数据、Node 22.22.2、Codex 0.147.0、独立 Pillow 环境已预复制并验证。
没有启动生产服务，没有正式复制/激活 auth-source，也没有生成测试素材。

一次仅含必要访问字段的只读模型目录预检：美国 HTTP 200 且 `gpt-5.5` 可见，
香港 HTTP 403、响应非 JSON。现有证据不能区分地区、账号或 WAF 原因。
预检凭据片段已在两端删除；没有 OAuth 刷新、登录、代理或正式授权状态变更。
香港尚未满足上游访问验收条件，因此美国广告与视觉保持运行。

## 等待条件与清单外发现

1. 香港剧集 `679e7c49acbf4af79f78bf60d76c5dd7` 仍在自然渲染。
   CPU 已记录请求失败，但香港子进程和输出文件仍在推进，不把 CPU 失败状态视为排空。
   原计划要求等剧集完成后移动共享目录；未得到用户调整确认前继续遵守。
2. 两条 CPU 广告模板视频 cron（HotDrama、DramaWave）仍经 SSH 调用美国
   `/data/ad-material-template-production/gpu_ad_material_worker.py`。
   它们不属于已批准的 12 个服务，也不受本批 API gate 或 17 个 unit mask 约束。
   已向用户询问是否追加迁移；未获确认前不停止整个业务 cron。
3. 截图/封面/广告/视觉共用美国隧道；香港广告访问未通过时，不按原联合窗口
   贸然停止该隧道。拆分切换需要明确调整方案。

## 备份与回滚原则

CPU 批次根目录：`/mnt/data-disk/migrations/gpu-service-migration-20260828T1502/`。
香港和美国批次根目录：`/data/migrations/gpu-service-migration-20260828T1502/`。
授权、环境文件和数据库均仅保留服务器私有目录，不进入仓库或此报告。

任何回滚先关闭目标入口、暂停调度并确认请求排空，再恢复原端。
TikTok 如有新的发布事实，必须先回同步最新 JSON manifest 与发布账本；
CPU SQLite 与短码保持最新，禁止恢复旧快照覆盖新事实。
X 同样先保留目标新增 manifest，再决定是否切回美国；已有未知发布结果不得重试。

本报告将随实际阶段更新。未完成的验证不得以代码已提交、预复制完成或
单项健康检查替代；最终人工验收由用户进行。
