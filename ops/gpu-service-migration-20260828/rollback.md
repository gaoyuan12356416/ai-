# 逐项回滚操作边界

批次 `gpu-service-migration-20260828T1502`。以下是需要时执行的操作步骤，
**本次没有执行回滚，也没有授权后台自动回滚**。
先核对 `execution-report.md` 中实际完成的阶段；未切换的业务不需要回滚生产。

## 共同前置检查

1. 使用本批已发布、已核对 SHA 的 checkout，保留当前日志、unit、配置和数据指纹。
   CPU 备份根目录为 `/mnt/data-disk/migrations/gpu-service-migration-20260828T1502`，
   美国和香港分别为 `/data/migrations/gpu-service-migration-20260828T1502`。
2. 在 CPU 执行 `control/maintenance.py gate-on GROUP --apply`，再执行
   `control/maintenance.py pause GROUP --apply`；GROUP 为 `x`、`tt` 或 `materials`。
   若已暂停且 journal 未恢复，先检查现状，不重复覆盖首次触发器快照。
   保持查询、短码解析、GPU 回调及不相关业务开放；不重启生产主 API。
3. 等调用方、HTTP 请求、工作线程、所有线程下的子进程及有效/过期 claim 排空。
   timer/path 停止并不等于当前 runner 停止。不能强杀在途媒体处理或发布。
4. 重新备份最新数据并逐文件核对。未知发布结果须先按原业务对账规则人工处理；
   迁移工具不能重试、强制改状态、恢复旧数据库或人工补跑错过时段。
5. 在目标端完全停止、监听释放并确认没有后台工作之前，不能恢复美国同一业务。
   恢复时只恢复原先 active/enabled 的服务及触发器；不要把整个主机全部 enable。

## X：优先仅撤回香港运行环境

**后续版本保护：** 17:34 另一个获授权任务将香港 X 升级为
`170e3b1325b71a72fcd6de913982ce92bb77fa40`，保留了本批 `/data` unit。
下面的原迁移回滚命令只适用于原 `fba8ff6` 基线；当前不能直接执行它恢复旧 unit，
否则可能同时撤回后续下载修复。必须先与该版本所有者核对其
`/data/x-post-media-repair/backups/20260828-pool-blockers-download/` 备份，
共同冻结要保留的业务版本、最新状态和对应运行环境，再发布精确的组合回退配置。
未完成该核对前保持现行170e3和美国mask；unit SHA相同不代表业务代码没有变化。

1. 使用共同步骤对 `x` 关入口并暂停。CPU `control/x_drain.py` 必须 ready，
   并检查香港工作目录、进程和 HTTP 均排空。
   原有 `publish_log/queue=726` 未知结果继续原样保留，不能为通过检查而删除。
2. 记录香港 X worker 和依赖隧道的当前 unit SHA、active/enabled、PID。
   停 worker 会因 `Requires=` 同时停隧道，须把这一状态变化计入操作记录。
3. 归档 `/data/x-post-media-repair/state` 最新全部 manifest。
   若恢复原 unit，先从原 `/etc/x-post-media-repair.env` 确认其工作目录，
   将最新 manifest 集合校验后放入该目录，旧集合另存；不能只恢复迁移前的 71 条。
   CPU 两套 SQLite 始终保留当前版本。
4. **仅在上述版本前提已满足时**，从已核对 checkout 执行原迁移回退：

   ```sh
   /usr/bin/python3.9 hk/deploy.py rollback --component x \
     --cutover-approved gpu-service-migration-20260828T1502 --upstream-paused
   ```

   此命令只恢复香港原 unit，不启动美国。若 unit 已被其他部署修改，脚本拒绝覆盖。
   本次实际激活使用早期 `7c54dedd` 控制器，没有后版的
   `x-tunnel-dependency-baseline.json`；须结合
   `hk/x-tunnel-before-restart.json`、`hk/x-tunnel-restored.json` 和原 unit SHA，
   在 worker 健康后**显式启动原本 active 的香港 X 隧道**。
5. CPU 对 `18820` GET 健康检查并检查监听 sshd 的远端为香港，验证 profile、
   最新 manifest 集合、环境和临时目录，再执行 `maintenance.py resume x --apply`
   及 `maintenance.py gate-off x --apply`。不要只看香港本地 worker active。

如必须切回美国，则保持香港 worker/tunnel 停止，将香港最新 manifest 先安全归档、
核对并回传到美国；按下方“恢复美国 unit”操作。香港与美国不能同时持有 CPU18820。
保留 `/data` 新状态及 FB 仍使用的 `/opt/x-post-media-repair/venv`，不删除环境。

## TikTok：发布事实优先

1. 对 `tt` 关闭入口并暂停全部七个原触发器。使用 `tt/cpu_state.py snapshot
   --samples 3 --require-drained --require-paused`，并检查目标 GPU 无线程、子进程、
   请求和未知发布结果。未来未 claim 的 ready 任务允许保留，不改发布时间。
2. 先停止香港两条 TT tunnel；确认 GPU 工作已自然结束后停止两个 worker。
   对四个 unit 检查 inactive、MainPID=0、端口释放。断隧道本身不终止 GPU 请求。
3. 在线备份当前 CPU 两套 SQLite，保留当前短码与全部发布事实；不恢复任何旧库。
   归档香港两 lane 的四个 JSON 目录、资源及必要媒体，并逐文件 SHA 校验。
   必须包含 `published`、`failed`、`init_rejected` 等所有已产生账本，不能只拷成功记录。
4. 如果目标曾产生任何新发布事实，先将香港最新四目录的**精确集合**回同步到美国，
   将美国旧集合完整归档。不能把本次初始 export 或 precopy 当作最新数据，
   不能用 overlay 留下已删除的旧 JSON，也不能恢复旧快照覆盖新 publish_id。
   未知结果未清楚时维持维护状态，禁止启动美国重试。
5. 如仅撤回目标配置，可执行 `bash tt/rollback-target.sh
   gpu-service-migration-20260828T1502`。它要求目标四 unit 全停，只恢复配置/unit，
   不恢复账本、不删除媒体、不启动任何端。
6. 完成最新事实回同步后按下节恢复美国四 unit。先 worker、再各自 tunnel，
   CPU18830/18834 的 GET health、profile、资源指纹、冻结 URL 和账本集合全部核对。
7. 最后恢复原七个触发器及公网入口。禁止 run-now、提前发布或人工补跑。

## 截图、封面、广告、视觉联合撤回

1. 对 `materials` 关闭新建/重试入口，包括全部方法的精确 GET batch 路由；
   暂停两测试服务和原截图自动 cron。等待所有相关任务和香港剧集自然完成。
2. 记录新产物及下载地址。用新鲜维护证明执行 CPU `migrate_cpu.py stop`，
   香港 `deploy.py rollback --component ad` 使用同一显式维护参数。
   核对 CPU18790/18795/18798 与香港 ad worker/tunnel 完全停止。
3. 优先保留 CPU 已切换的数据盘兼容链接。`rollback-storage` 只允许未新增数据、
   与切换前 manifest 完全一致的情况；有新文件时拒绝回盘，不能强制覆盖。
   原下载 URL、成功尺寸和失败尺寸重试结果均应保留。
4. 按下节仅恢复原美国生产 worker 及共享/突发隧道。三个废弃独立尺寸实例如
   无生产需求，不作为容量恢复手段重复启用。保持四个有效截图槽及原 API 地址。
5. 验证三个截图尺寸、封面、广告、视觉、历史下载和端口归属后，再恢复原入口。
   香港剧集18788及 FB 不参与重启。

## 恢复美国 unit 的逐项步骤

源端的 `source-fence/GROUP-before.json` 保存本批源 unit 清单、原状态和原路径；
每个 unit 子目录保存 `original.service`、`retired-local.service` 和存在时的 dropins。

1. 核对香港/CPU对应新端已停止、数据已按上述规则回同步、维护证明仍新鲜。
2. 对清单内每个 unit 检查现为 masked、MainPID=0；若不是本批建立的 `/dev/null`
   mask 或配置已经漂移，停止操作并人工比对，不覆盖他人修改。
3. 逐个解除本批 mask，按快照原 fragment 位置恢复已验证的 unit 文件；
   如原本是系统 vendor unit，仅解除 mask，不复制为新的 `/etc` unit。
   对 dropins 逐文件核对，不能整目录覆盖其他新增配置。
4. `systemctl daemon-reload`，逐个 `systemd-analyze verify`。按快照恢复 enabled 状态，
   然后只 start 原本 active 的 worker，最后 start 原本 active 的对应 tunnel。
5. 检查 CPU 端口归属、源端单一 worker、健康接口、最新数据指纹及原触发器状态；
   所有验证通过才解除维护入口。保留本次与回滚的全部备份，不自动删除服务器。
