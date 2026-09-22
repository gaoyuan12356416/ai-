# Meta 删除吞吐调整

本次将删除执行调整为全局最多 8 个并发操作、同一广告账户 1 个，精简执行中的 SQL，并对明确拒绝 Video ID 的结果合并读取账户视频库。减少等待的同时，保留冻结范围、发送前台账、成功回执和未知结果隔离。

## 运行行为

- 8 个操作名额由当前主 API 进程内的所有删除任务共用，账户锁也跨任务共享。每个账户工作线程使用独立的 Graph/HTTP 客户端。同账户串行，多个账户并行；Creative → Ad → Video 的阶段顺序保持。
- 此限额针对现有单主 API 进程拓扑。不能直接扩为多个 API 进程或实例并声称全局仍为 8；多进程需要额外的跨进程调度。
- 每次写入前仍重新核查 Cookie 登录、模块和产品权限。选中产品检查只查询当前明确选中的产品，不遍历全部可见产品；不缓存权限判定。
- Video 的冻结源广告、发布队列、产品默认用户和指定用户 Token 合并为一次精确 SQL 查询。每次账户尝试都重新查询，保留 `default_token=1` 使用当前产品默认用户、`-1` 使用发布用户的规则，以及队列/广告/产品/用户的一致性检查。异常不退回其他凭证。
- 本模块的来源 SQL 最多 4 个同时进入既有查询通道，继续使用现有 FIFO 门控和只读 63350 数据源。8 和 4 为本版固定值，不修改 `.env`。
- 同账户当前批次中，只有已经发送 DELETE 且明确返回 `100 / Param video_id is not a valid video ID` 的结果进入合并核查。先完成该账户工作批次的删除，再按同一账户及完全相同的实际 Token/凭证身份合并分页读取。不同 Token 不共享核查结果；凭证只在内存中用于分组。
- 完整分页且确认某个 Video 不存在时，只为该账户/Video 写入 `already_deleted` 与账户回执。仍存在、权限失败、超时、分页不完整等情况保留原 DELETE 失败及核查诊断。合并读取不是下一批可复用的缓存，也不是 DELETE 前置条件。
- 合并核查前先持久化明确拒绝的结果，并保留活动账户尝试和父对象锁。若进程退出，已持久化拒绝的尝试收敛为 `failed` 并标注核查中断；真正请求在途的尝试仍为 `unknown`；未发送账户保留 `pending`；已成功账户保留回执。恢复必须由用户正常执行入口发起，不自动重放删除。
- HTTP 429、受支持的限流错误码、Meta 用量响应头和 `Retry-After` 可触发账户或全局冷却。后续请求等待冷却，不自动重发当前失败请求或轮换 Token。用量达到 80% 起轻度等待，95% 起加长等待；这些是保守调度参数，不代表 Meta 承诺的固定并发额度。
- 台账或授权失败停止调度新的写入，等待已开始的工作结束，再终结任务；保留逐对象和逐账户结果。没有删除范围扩展，没有历史成功/失败批量改写。

## 基线和验证

基线提交为 `9c2178fa7b08899e3ffcf360dc114c44596ae76c`。该提交已经纳入生产中部署的预览扫描和查询容错修正，避免提速发布覆盖现有预览能力；其余模块延续 `e3411b7e93434ca5e21285fc81dd9acdce842ea8` 的账户 Video Token 路由规则。上线前逐文件比对实际运行字节，不以旧部署记录代替现场检查。

针对性测试覆盖全局 8 / 同账户 1、跨任务共享额度、阶段顺序、当前权限、精确 SQL 凭证路由、合并核查的分页与 Token 隔离、恢复状态、回执和凭证隐私。测试使用临时台账与模拟传输；不以真实 Meta DELETE 作为验收探针。

从精确 GitHub release 执行：

```bash
cd "$release_dir"
python3 -m unittest discover -s tests -p 'test_fb_ad_asset_delete*.py' -q
python3 -m py_compile features/fb_ad_asset_delete/bridge.py features/fb_ad_asset_delete/execution.py features/fb_ad_asset_delete/graph.py features/fb_ad_asset_delete/service.py features/fb_ad_asset_delete/source.py features/fb_ad_asset_delete/store.py scripts/deploy_meta_delete_throughput.py
git diff --check
```

2026-09-22 10:45（北京时间）已发布并验收。运行提交 `00f55957433db24de79778d6c3d99cc966894e4d`，分支 `codex/meta-delete-throughput-20260922`，GitHub 推送及服务器精确 fetch 均已核对。本地及服务器均通过 354 项模块测试，服务器运行 Python 3.9.6；服务器 SQLite 3.26 的测试方言差异仅在测试适配器中修正，生产 MySQL SQL 未改变。

- Release：`/mnt/data-disk/meta-ad-asset-delete/release-00f55957433d`。
- 备份：`/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260922T024404Z-00f55957433d`，其中 `acceptance.json` 保存本次验收、表行数及完整行摘要，`plan.json` 保存全部 6 个目标文件的前后哈希。
- 主 API PID `3608346 → 1163352`，active；剧合成 worker PID `3608348` 未变化；全部原有 35 个 active timer 仍 active。启动日志没有导入、语法或 traceback 错误。
- 6 个运行目标哈希与 release 一致；运行代码预算为全局 8、来源 SQL 4。页面/topbar/auth-status 返回 200，未登录删除 products/jobs 返回 401，原任务所有者只读 GET 返回 200、保留原 partial 状态。
- 台账完整性及外键检查通过，所有历史表逐行摘要与备份完全一致：24 jobs、10 runs、10,668 objects、2,027 object attempts、1,448 receipts、1,077 video-account rows、1,080 video-account attempts、7,750 audits；没有新增删除请求或台账回写。
- 两次只读线上 SQL 对比共覆盖 4 个冻结账户/视频对，包含 `default_token=-1` 和 `1`。新旧安全上下文及内存 Token 摘要比较全部一致，实际凭证查询由 2 次降到 1 次；所选产品查询返回由 329 行降到 4 行。EXPLAIN 通过，凭证来源及队列/产品使用主键、Token 使用用户唯一索引。报告在 `/mnt/data-disk/fb-ad-asset-delete/throughput-source-probe-20260922T024203Z.json` 和 `throughput-source-probe-20260922T024404Z.json`。
- 模拟传输集成测试验证了 12 次账户 DELETE 后用 1 次读取完成合并核查，超时对象不进入明确拒绝核查、不自动重发。生产验收 Meta DELETE 数为 0；实际业务提速倍数留待下一次正常授权任务观察。

## 选择文件发布

主机 `43.166.187.96`，运行入口 `/root/drama_material_service`，保留既有数据盘符号链接。发布只包含 `features/fb_ad_asset_delete/` 下的 `bridge.py`、新增 `execution.py`、`graph.py`、`service.py`、`source.py` 和 `store.py`。不发布全仓库，不修改公共静态文件、共享导航或其他业务模块，无 SQLite schema 迁移。

先提交并推送已验证代码，再在 `/mnt/data-disk/meta-ad-asset-delete/release-<SHA前12位>` 检出同一个 GitHub SHA。以下 `release_dir` 与 `backup_dir` 必须替换为本次确切路径，不能套用其他发布的备份。

1. 检查服务拓扑。主 API 使用独立剧合成 worker；X/TT/FB 发布若仍使用独立 release 与 8810/18831/18835 等 sidecar，不停止无关发布 timer。记录其状态并在发布后核对。拓扑改变时先确认受影响依赖。
2. 确认删除、预览和重核均空闲，不打断现有工作。运行 `prepare`，它验证数据盘挂载 UUID、干净 GitHub release、逐文件基线，并在线备份 SQLite 和原代码。
3. 停止主 API，应用文件，再通过已有 worker-aware helper 启动。`apply` / `rollback` 拒绝在主 API 未停止（`ActiveState=inactive` 且 `MainPID=0`）时操作，并再次验证台账空闲和文件哈希。若检查失败，先排查原因；不要重置任务或修改基线绕过检查。

```bash
release_dir=/mnt/data-disk/meta-ad-asset-delete/release-<本次SHA前12位>
python3 "$release_dir/scripts/deploy_meta_delete_throughput.py" prepare "$release_dir"
# 将 prepare 输出中的 backup 路径填入下一行。
backup_dir=/mnt/data-disk/meta-ad-asset-delete/recovery-backup-<本次时间戳>-<本次SHA前12位>
systemctl stop drama-material-api.service
python3 "$release_dir/scripts/deploy_meta_delete_throughput.py" apply "$backup_dir"
python3 -m py_compile /root/drama_material_service/features/fb_ad_asset_delete/bridge.py /root/drama_material_service/features/fb_ad_asset_delete/execution.py /root/drama_material_service/features/fb_ad_asset_delete/graph.py /root/drama_material_service/features/fb_ad_asset_delete/service.py /root/drama_material_service/features/fb_ad_asset_delete/source.py /root/drama_material_service/features/fb_ad_asset_delete/store.py
bash "$release_dir/scripts/safe_restart_drama_api.sh"
systemctl show drama-material-api.service --property=ActiveState --property=MainPID
journalctl -u drama-material-api.service -n 100 --no-pager
```

验收至少确认运行文件与 release 哈希一致、主 API 正常、启动日志无导入异常、页面和 topbar 可读、未登录 products/jobs 返回 401、原任务所有者只读 GET 正常、SQLite 完整性及所有旧行保持、无关 timer 和服务未受影响。上述步骤不得创建或重试实际删除任务；运行性能由后续正常授权任务观察。

## 回滚

本次回滚直接恢复已知可用的基线文件，无需旧版本的 Video 禁写补丁。先排空删除/预览/重核，再停止主 API。回滚只恢复代码，绝不恢复旧 SQLite，保留当前成功回执、失败、未知结果及锁。新增的 `execution.py` 保留原精确字节，旧入口不会导入它；备份 manifest 允许相同备份再次 apply 时匹配该精确哈希，任意漂移仍会拒绝。

```bash
release_dir=/mnt/data-disk/meta-ad-asset-delete/release-00f55957433d
backup_dir=/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260922T024404Z-00f55957433d
# 确认删除、预览、重核空闲后执行。
systemctl stop drama-material-api.service
python3 "$release_dir/scripts/deploy_meta_delete_throughput.py" rollback "$backup_dir"
bash "$release_dir/scripts/safe_restart_drama_api.sh"
systemctl show drama-material-api.service --property=ActiveState --property=MainPID
```

恢复本次新版本时，按同一空闲/停止流程将 `rollback` 改为 `apply`，使用同一 release 与同一备份；重新执行哈希、服务和台账验收。
