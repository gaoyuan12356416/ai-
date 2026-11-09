# 三产品 AI 图片产出与投放日报

独立只读日报，覆盖 DramaWave（1479）、MoboShort（2373）、MoboReels（3360）及各自 W2A。业务日使用后台日表 `dt`，币种美元，不重算广告账号当地自然日。产出按来源产品、效果按实际投放应用归属；近七天重新读取归因，已发布历史快照不覆盖。

## DramaWave 人工与 AI 图片对比

首页及 DramaWave 视图新增独立对比，支持昨日、近七天累计及逐日数据，展示有消耗素材数、消耗、AF D0 收入、ROAS、曝光、点击、安装、CTR、CPI。原三产品 AI 统计保持原口径。

- 人工候选来自 `ads_custom_source.product='Dramawave'`、`type=1`、正式来源 `data_source IN (0,6)`；按正式 ID 去重，保留已删除图片历史。人工来源为素材任务 `task_id>0`、手动上传 `0`、Excel 导入 `-1`。
- 优先排除已验证 AI ID、相同 AI 图片签名、未解决来源 ID、`remark.origin=ai-cover`、`remark='ai drama'`、AI 接口导入 `task_id=-2` 和其他未知来源。相同签名但正式 ID 尚未验证的图片仍为待核对，不冒充已验证 AI。其他未知、AI 导入且来源未验证的实际消耗单列展示。
- 这是系统来源分类，不能证明人工设计人员未使用外部 AI 工具；不以“大盘图片减 AI”的余数充当人工。库存按日报日截断，原始分类集合冻结到 `human-inventory.json`。
- 两类效果均限定实际投放到 DramaWave 主 App/W2A。AI 沿用已验证正式 ID；人工以日表 `resource_id` 关联正式图片。查询与既有 AI、大盘数据同一每日只读事务，继续使用 SQL Gate；每个日期有独立覆盖标记，查询失败不能变成零。
- 默认比较全部投放，也可切换到两类同日均有消耗的共同应用/渠道。七日素材数对正式 ID 去重；ROAS/CTR/CPI 按总额重算。新增人工库存不合并进原 AI 产出或跨产品合计。

已发布当日需补充新口径时，使用 `--revision --date YYYY-MM-DD --prepare-only`，检查后仍以 `--publish-prepared` 发布冻结批次。只允许修订最新已发布日期，生成 `daily/YYYY-MM-DD/revisions/<version>/index.html`；保留原 `daily/YYYY-MM-DD/index.html` 和历史主条目，将修订链接追加到该日期 `revisions`。最新入口更新，历史选择同时提供原版与修订版。普通每日执行不传 `--revision`，已成功日期仍直接跳过。

## 运行与准备

```bash
/usr/bin/python3 -B /opt/ai-image-daily/current/ops/ai-image-daily/report.py \
  --state /mnt/data-disk/ai-image-daily-v3 \
  --env-file /root/drama_material_service/.env
```

现有 `ai-image-daily.timer` 保留北京时间 09:00、09:10、09:30 三次触发（后两次仅补偿）、`Persistent=true`，不新增调度器。独立 v3 状态目录避免旧版同日发布记录误跳过。成功后同日触发不查询或上传；存在 pending 时仅恢复冻结批次，不重新查数。多个进程使用同一文件锁。

首次上线使用相同命令加 `--prepare-only`：完成真实只读查数、校验、缩略图缓存与全部文件冻结，不进行 COS 写入。再次 prepare 复用已冻结 pending。审核 `runs/<run>/report.json`、`lineage.json`、`facts.json`、`validation.json`、`manifest.json` 后，用 `--publish-prepared` 发布同一批次；没有 pending 时此命令失败，不自动生成另一批数据。

## 静态资源和发布事务

- 新固定入口：`https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/ai-image-daily/index.html`。
- `daily/YYYY-MM-DD/index.html` 为不可变历史快照；`history.json` 独立更新。明细按产品延迟读取 `assets/<version>/details-<product>.json`；首页包含小体积摘要与趋势。
- HTML、JSON、JS 以 `gzip(mtime=0)` 存储并设置 `Content-Encoding:gzip`；版本化资源和历史快照长期缓存，最新入口、历史目录和 health 为 `no-cache`。
- 仅旧路径 `codex-artifacts/html/dramawave-ai-image-daily/index.html` 可写为保留 hash 的跳转页；旧 `daily/` 全部保留。发布器对路径做严格前缀和规范化检查。
- 所有未压缩/已压缩字节、SHA256、内容类型与缓存头先写入本地冻结清单。发布前完整检查不可变对象冲突，保存全部既有可变对象原始字节和元数据；先上传并公网验证全部资产，再验证历史页、历史目录、新首页，最后更新旧入口。
- 每个对象同时校验 COS 存储字节、HTTP 响应头、gzip 解压后字节。未知结果保留 pending；重试仅回读和补齐本批次，禁止重新生成或覆盖不同的已发布历史快照。可变对象发生非本批次漂移时停止。
- 独立 `status.json` 是实时健康状态，不属于冻结内容事务：成功记录日期/版本；失败尽力记录异常类型和上次成功日期，不含原始异常、SQL、URL或凭证，保留上次报告。prepare-only 失败不写公网。健康状态变化不影响 pending 或内容回滚校验。

缩略图只访问 `advertising-1306474899.cos.ap-hongkong.myqcloud.com`，规范为 HTTPS，去掉查询和片段，禁止重定向、userinfo、非标准端口。每产品优先实际投放消耗前 25 张，再合并来源为该产品的昨日新增，最多 100 张。相同 URL 去重，最多 4 并发、单次 10 秒、单图 10 MiB、总预算 90 秒；输出最长边 160px WebP。下载和生成失败保留占位与手动原图链接。缓存位于 state/thumbs，公网对象名按缩略图内容 SHA256，不复用根盘临时文件。

## 存储和查询边界

| 用途 | 位置 |
| --- | --- |
| 精确 SHA 代码 | `/mnt/data-disk/deploy/ai-image-daily/releases/<40位SHA>` |
| 代码入口 | `/opt/ai-image-daily/current`，指向 release 的符号链接 |
| v3 状态、冻结批次、回执、备份、缩略图 | `/mnt/data-disk/ai-image-daily-v3` |
| 原日报状态（保留） | `/mnt/data-disk/ai-image-daily` |
| 安装回滚点 | `/mnt/data-disk/deploy/ai-image-daily/backups/<UTC时间>` |
| 后台源 SQLite | `/root/drama_material_service/data/drama_material_jobs.sqlite3` |
| YouTube 入库账本 | `/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3` |
| 新接口逐图账本 | `/mnt/data-disk/ai-cover-material-source/upload.sqlite3` |

运行前验证数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、可写性及至少 1 GiB 空间，失败不写根盘。凭证仅读取现有 `.env`。MySQL 使用固定只读端点并经 `/usr/bin/mysql` SQL Gate，禁止直接驱动或绕过 FIFO Gate。SQLite 使用 `mode=ro` 和 `query_only`；systemd 保持源数据库只读，仅给予 WAL 读者必要旁文件访问。服务超时 1800 秒、内存上限 768 MiB；不修改素材/广告发布队列。

## 精确版本部署与回滚

本地检查并推送 GitHub 后，服务器 fetch 完整 commit，导出到对应 release。先停用原 timer，等待 service 空闲，再安装：

```bash
python3 -B <release>/ops/ai-image-daily/deploy.py \
  --repo <服务器已fetch的Git仓库> --expected-commit <40位SHA>
```

部署器逐文件比对 Git blob、编译 Python、验证 units，备份旧代码指针、units、状态小文件；仅切换日报，不启动 timer。首次可以在安装前从 release 运行 prepare-only；确认报告、发布与公网回读后，`systemctl enable --now ai-image-daily.timer`。只运行这一套调度。

```bash
python3 -B <本次release>/ops/ai-image-daily/deploy.py \
  --rollback <本次安装返回的backup目录>
```

代码回滚检查当前 release/unit 未漂移，停用日报 timer/service，恢复旧代码和 units，保留所有本地历史、公网资源与新版状态。恢复原 timer 必须在验证后显式 enable。

若还需恢复前一版公网入口，先停 timer，再使用本次 release：

```bash
python3 -B <本次release>/ops/ai-image-daily/report.py \
  --state /mnt/data-disk/ai-image-daily-v3 \
  --env-file /root/drama_material_service/.env \
  --restore-public <state/backups/本次发布备份目录>
```

回滚前检查所有可变对象与安装/原始 SHA256 和元数据一致，再恢复已存在对象的原字节与头部；任一漂移时拒绝操作。新建对象、全部历史快照和版本化资产不删除。首次升级恢复旧入口后，新入口仍保留为可访问快照。实时 health 不参与内容漂移校验；恢复既有 v3 首页时同步备份的上次成功状态，首次升级则保留新首页对应状态。

## 本地验证

```bash
python -B -m unittest discover -s ops/ai-image-daily -p '*test*.py' -q
```

发布测试完全使用内存对象，覆盖冻结篡改、gzip与缓存头、未知结果恢复、不重复上传/查询、历史冲突、跨日保护、可变对象漂移、完整回滚及路径范围；缩略图验证主机限制、跨产品榜单、数量限制、真实尺寸、去重缓存与预算失败。
