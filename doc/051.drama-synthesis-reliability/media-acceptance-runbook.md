# 剧集合成媒体验收操作手册

更新：2026-08-28。**本手册的短片、CPU参数对照、完整解码及90分钟长片验收均未执行；不是上线或性能通过证明。** 使用工具为候选提交内的 `scripts/benchmark_drama_synthesis_media.py`。发布顺序和备份规则见 [deploy.md](deploy.md)，结果写入 [test-report.md](test-report.md)。

## 1. 执行前提与当前阻塞

当前不能启动渲染验收。旧正式渲染已在约17:36:51自行退出，17:41核对没有视频成片；GPU服务未重启、没有OOM证据。时间与旧10800秒限时吻合，但原异常未保留，超时原因仍标为推断，见 [原任务证据](evidence/original-case-outcome-20260828.json)。没有执行终止操作，也没有收到终止授权。主机内存随后恢复不等于获得测试窗口：迁移任务 `gpu-service-migration-20260828T1502` 正在交接TT，须等其确认端口交接和HK媒体窗口后才可启动本手册中的任何媒体命令。

启动验收必须同时满足：

1. 旧任务自然结束并核对结果；或用户明确授权终止旧任务、已保存所需证据，并确认原PID及其进程组全部停止。**仅发出停止请求不满足前提。** 不提供针对旧任务的 `kill`、重启或清锁捷径。
2. 通过已批准维护流程暂停新的正式drama制作入口，并确认队列、租约和制作进程排空。不能仅凭页面“空闲”判定，也不能靠删除账本排空。
3. 同机FB服务保持原配置及优先级。基线记录为FB `Nice=0`、drama `Nice=10`；按现场实际unit复核，不停止FB服务、不降低其配额，不做真实FB发布来测影响。
4. 候选已经审查并推送GitHub，记录完整40位提交SHA；从GitHub准备独立、干净的候选checkout。**源码基线 `420957be4c288308c38b97f773be330208887204` 不是本轮候选SHA。** 不修改生产 `current`、正式unit或正式环境文件来做对照。
5. 维护窗口内一次只有一个隔离渲染；脚本本身**不占正式GPU制作锁**。下文 `flock` 只防止这些benchmark互相并行，不能替代第2项。
6. 与“统计GPU服务器运行任务”确认TT已完成端口交接、HK媒体窗口可用；CPU公共目录/主API或HK剧集部署须再次协调，不能因隔离测试获准而自行切换生产。

隔离保护预算在执行前由验收负责人确认并记录。已知本机为systemd `239-51.el8_5.2`、kernel `4.18.0-348.7.1.el8_5`、cgroup v1；执行前重新核对。建议开始时 `MemAvailable >= 24 GiB`；测试unit使用该版本支持的 `MemoryLimit=16G`、`TasksMax=128`。运行中主机可用内存低于8 GiB、FB开始受资源影响、出现OOM或任务数触顶，立即停止**本次隔离unit**并判本轮未通过。预算不足时延期，不通过放宽阈值掩盖持续增长。上述是验收保护值，不是生产配置变更。

**交换保护尚未落实，是额外的执行门禁。** v1下不能把 `MemorySwapMax=0` 写进命令后当作禁用交换已生效；`MemoryLimit` 本身也不限制全部RAM加swap。必须由主任务在执行前确认并记录适用于本机、在测试进程启动前已生效的独立cgroup保护及真实 `memory.memsw.limit_in_bytes` 读回；如果该文件缺失或无法无竞态设置保护，保持阻塞，不运行下面模板。memsw是RAM与swap合计限制，即使与memory limit相等也不能声称绝不使用swap。不得用启动后补写限额、全机swapoff、变更FB限额等办法绕过门禁。下文确认变量只是操作员记录，不提供任何实际保护机制。

## 2. 已保留输入与冻结记录

| 项目 | 已知记录 / 执行前动作 |
| --- | --- |
| 长片硬链接 | `/data/drama-synthesis-gpu/acceptance/20260828-reliability/inputs/case-679e7c49-concat.mp4`；17:41原渲染结束后复核仍存在，大小 `5139047136` 字节；此前与原concat核对同一inode |
| 原concat路径 | `/data/drama-synthesis-gpu/work/jobs/679e7c49acbf4af79f78bf60d76c5dd7/8HehaA3263_679e7c49_eps_1_70.mp4`；正常清理后可能消失，验收只读保留路径 |
| 源SHA256 | **尚未计算**；排空后计算、记录，不能把文件大小或同inode当作SHA |
| 长片时长 | 旧命令记录约 `5573.906333` 秒；验收前重新ffprobe，以实际源时长冻结；长样必须5400～7200秒 |
| 已冻结配方 | 本地受限文件 `output/case-679e7c49-recipe.json`；经核对后将同一JSON放入GPU输入目录，不能重新随机生成 |
| 配方内部指纹 | `recipe_sha256=56d60ff057e0da8eb08b0ef8063be0ef75d37d28a970c1c912ad915f8de9793f`；这是规范化配方指纹，**不是JSON文件字节SHA**，文件SHA另算 |
| profile | `drama-random-overlay-h264-720x1280-v1`，`source=concat_video` |
| 素材根 | `/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114` |
| 素材manifest指纹 | `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f` |

**不得对硬链接执行chmod、chown、截断、重写或“修复”。** 它与原文件共享inode，修改会影响原文件。需要权限时只调整经审核的新证据目录或独立配方副本；不能递归修改输入目录。原路径被正常unlink后，保留链接仍可使用，不要求此时链接数仍大于1。

所有短样使用同一配方、素材manifest、ffmpeg/ffprobe二进制和同一源SHA；长样使用原约93分钟源，不以循环短片凑够90分钟，不重新编码源来回避分辨率/集边界问题。保持原片头、集序、25fps标准化语义、30fps模板、NVENC/profile和滤镜顺序。

## 3. 候选目录、输入和证据准备

以下为**未来维护窗口中的Linux命令模板，本次未执行**。先填写候选SHA和新的批次标识；不存在的配方副本、用户目录或依赖不得靠临时改生产配置绕过。候选checkout按部署流程从GitHub取得，不能把本地未提交源码直接覆盖到服务器。

```bash
set -euo pipefail
umask 077
TASK_CANDIDATE_SHA='REPLACE_WITH_REVIEWED_40_HEX_SHA'
TASK_RUN_TAG='REPLACE_WITH_NEW_LOWERCASE_TAG'
TASK_SWAP_GUARD_CONFIRMED=no
[[ "$TASK_CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$TASK_RUN_TAG" =~ ^[a-z0-9-]{1,30}$ ]]
test "$TASK_SWAP_GUARD_CONFIRMED" = yes

TASK_ROOT=/data/drama-synthesis-gpu/acceptance/20260828-reliability
TASK_INPUTS="$TASK_ROOT/inputs"
TASK_CODE="/data/drama-synthesis-gpu/acceptance/code/$TASK_CANDIDATE_SHA"
TASK_RUN_ROOT="$TASK_ROOT/runs/$TASK_CANDIDATE_SHA-$TASK_RUN_TAG"
TASK_CONTROL=/data/drama-synthesis-gpu/acceptance/control
TASK_LONG="$TASK_INPUTS/case-679e7c49-concat.mp4"
TASK_SHORT="$TASK_INPUTS/case-679e7c49-intro-first120s.mp4"
TASK_RECIPE="$TASK_INPUTS/case-679e7c49-recipe.json"
TASK_ASSETS=/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114
TASK_MANIFEST=028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f
TASK_PYTHON=$(readlink -f /data/drama-synthesis-gpu/runtime/current/bin/python)
TASK_FFMPEG=$(readlink -f /data/drama-synthesis-gpu/runtime/bin/ffmpeg)
TASK_FFPROBE=$(readlink -f /data/drama-synthesis-gpu/runtime/bin/ffprobe)

test "$(git -C "$TASK_CODE" rev-parse HEAD)" = "$TASK_CANDIDATE_SHA"
test -z "$(git -C "$TASK_CODE" status --porcelain)"
test -r /proc/self/cgroup
test -r /proc/self/mountinfo
test -f "$TASK_LONG"
test ! -L "$TASK_LONG"
test -f "$TASK_RECIPE"
test ! -e "$TASK_RUN_ROOT"
test ! -L "$TASK_RUN_ROOT"
install -d -m 0700 -o drama-synthesis-gpu -g drama-synthesis-gpu "$TASK_RUN_ROOT"
```

`TASK_CONTROL` 是所有批次共用的benchmark锁目录，由运维预先建立并确认 `drama-synthesis-gpu` 可写；**不要替换或删除已有锁文件**。候选代码、输入、素材和运行时须可由该用户读取；不得通过修改硬链接权限解决。预检须从 `/proc/self/cgroup` 和 `/proc/self/mountinfo` 确认v1的cpu（可与cpuacct合挂载）、memory、pids控制器及下文资源属性支持，不要求v2的 `cgroup.controllers` 文件。还须完成第1节交换保护门禁，并记录证据后才可将 `TASK_SWAP_GUARD_CONFIRMED` 改为yes；不能只改变量。缺失时暂停，不能无上限运行。

在维护窗口只读记录候选SHA、源大小/inode、SHA和运行时版本，记录到本批次私有文件：

```bash
git -C "$TASK_CODE" rev-parse HEAD > "$TASK_RUN_ROOT/candidate-sha.txt"
stat -Lc 'size=%s dev=%d inode=%i links=%h' "$TASK_LONG" > "$TASK_RUN_ROOT/long-source-stat.txt"
nice -n 10 ionice -c 2 -n 7 sha256sum "$TASK_LONG" "$TASK_RECIPE" "$TASK_ASSETS/manifest.json" "$TASK_FFMPEG" "$TASK_FFPROBE" > "$TASK_RUN_ROOT/frozen-inputs.sha256"
"$TASK_PYTHON" --version > "$TASK_RUN_ROOT/python-version.txt"
"$TASK_FFMPEG" -version > "$TASK_RUN_ROOT/ffmpeg-version.txt"
"$TASK_FFPROBE" -v error -show_streams -show_format -of json "$TASK_LONG" > "$TASK_RUN_ROOT/long-source-probe.json"
nvidia-smi --query-gpu=name,uuid,driver_version --format=csv,noheader > "$TASK_RUN_ROOT/gpu-version.txt"
```

核对recipe内部指纹、profile、素材manifest为表中值，并另核对JSON文件字节SHA。脚本会校验配方规范化SHA和完整素材清单，但不能替代执行前冻结记录。不要输出私密环境文件、URL、Token、COS凭据或全量进程参数。若GitHub发布流程使用无 `.git` 的导出目录，应先按部署产物manifest完成同等SHA核对，不能删掉Git检查后无证据继续。

短样只生成一次，保留开头片头和首次集边界，输出必须不存在。此命令只做本地stream copy；不改变源、不重编码、不按每个配置重复制作短样：

```bash
test ! -e "$TASK_SHORT"
test ! -L "$TASK_SHORT"
nice -n 10 ionice -c 2 -n 7 "$TASK_FFMPEG" -nostdin -hide_banner -loglevel error -n -i "$TASK_LONG" -t 120 -map 0:v:0 -map 0:a:0 -c copy "$TASK_SHORT"
chown drama-synthesis-gpu:drama-synthesis-gpu "$TASK_SHORT"
chmod 0400 "$TASK_SHORT"
"$TASK_FFPROBE" -v error -show_streams -show_format -of json "$TASK_SHORT" > "$TASK_RUN_ROOT/short-source-probe.json"
sha256sum "$TASK_SHORT" >> "$TASK_RUN_ROOT/frozen-inputs.sha256"
```

复制结果的实际时长可以有封装/关键帧舍入，必须仍在0.5～300秒范围。上面的chown/chmod只作用于刚由FFmpeg创建的独立短样，不得换成原硬链接或递归目录命令。若目录已有合格短样，先核对其冻结记录后复用，不能覆盖重建。

## 4. 隔离渲染命令与三组短样

函数只提交一个独立临时unit，**返回不代表完成**。测试用户、Nice及IO优先级沿用drama配置；不加载正式 `EnvironmentFile`，无需数据库、HTTP或COS凭据。公共锁只约束benchmark；正式入口仍必须维持排空。

```bash
launch_media_case() {
  local key="$1" quota="$2" threads="$3" kind="$4" source="$5"
  test "${TASK_SWAP_GUARD_CONFIRMED:-no}" = yes
  [[ "$key" =~ ^[a-z0-9-]{1,30}$ ]]
  [[ "$quota:$threads" == '200%:2' || "$quota:$threads" == '400%:2' || "$quota:$threads" == '400%:4' ]]
  [[ "$kind" == short || "$kind" == long ]]
  local out="$TASK_RUN_ROOT/$key"
  local unit="drama-media-accept-${TASK_CANDIDATE_SHA:0:12}-$TASK_RUN_TAG-$key"
  test ! -e "$out"
  test ! -L "$out"
  systemd-run --unit="$unit" --service-type=simple \
    --uid=drama-synthesis-gpu --gid=drama-synthesis-gpu \
    --property="WorkingDirectory=$TASK_CODE" \
    --property=RemainAfterExit=yes --property=KillMode=control-group --property=TimeoutStopSec=90 \
    --property=Nice=10 --property=IOSchedulingClass=best-effort --property=IOSchedulingPriority=7 \
    --property="CPUQuota=$quota" --property=TasksMax=128 \
    --property=MemoryLimit=16G \
    --property=CPUAccounting=yes --property=MemoryAccounting=yes --property=TasksAccounting=yes \
    --property=UMask=0077 --property=NoNewPrivileges=yes \
    --property="ReadOnlyPaths=$TASK_INPUTS $TASK_ASSETS $TASK_CODE" \
    --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=PYTHONNOUSERSITE=1 --setenv=PYTHONUNBUFFERED=1 \
    --setenv=OMP_NUM_THREADS=1 --setenv=MKL_NUM_THREADS=1 --setenv=OPENBLAS_NUM_THREADS=1 --setenv=NUMEXPR_NUM_THREADS=1 \
    /usr/bin/flock -n "$TASK_CONTROL/media-benchmark.lock" \
    "$TASK_PYTHON" "$TASK_CODE/scripts/benchmark_drama_synthesis_media.py" --apply render \
    --source "$source" --recipe "$TASK_RECIPE" --asset-root "$TASK_ASSETS" \
    --asset-manifest-sha256 "$TASK_MANIFEST" --output-dir "$out" \
    --sample-kind "$kind" --filter-threads "$threads" \
    --ffmpeg "$TASK_FFMPEG" --ffprobe "$TASK_FFPROBE" --timeout 43200
}
```

这里使用systemd239支持的 `Type=simple`；提交成功甚至不能证明程序已成功exec，必须读取后续状态和证据。命令只设置v1内存上限，不实现第1节的交换保护；该前提未落实时不得调用函数。

代码默认 `DRAMA_GPU_RENDER_TIMEOUT=43200`，即12小时；显式 `--timeout` 优先，同样要求60～86400秒。这里固定43200保证各组相同。**这是FFmpeg渲染进程时限，不是HTTP的4小时等待上限，不是整套验收耗时上限。** 超时仍由渲染器停止自己的进程并wait；本次测试不能临时增加时限后把中断样本算作通过。

依次运行下表每一行；前一轮确认退出、保存记录并完成第5节检查后，才运行下一行。不要并行粘贴执行。建议第二轮倒序，不能清系统page cache或重启FB来制造“公平”环境。

| 顺序 | 命令 | 目的 |
| --- | --- | --- |
| 1 | `launch_media_case short-2c2t-r1 200% 2 short "$TASK_SHORT"` | 当前配额基线 |
| 2 | `launch_media_case short-4c2t-r1 400% 2 short "$TASK_SHORT"` | 单独观察增加CPU配额 |
| 3 | `launch_media_case short-4c4t-r1 400% 4 short "$TASK_SHORT"` | 再观察滤镜线程变化 |
| 4～6 | 同三组使用新的 `r2` 名称，顺序4c4t→4c2t→2c2t | 识别缓存、温度和负载造成的波动 |

每次 `--output-dir` 必须是全新绝对路径，不能预建该子目录、拷入成片或检查点，也不能覆盖失败目录重跑。输入和配方可以复用，**输出不能复用**。脚本要求 `rendered_processes=1` 且有真实采样，否则不是有效性能样本。

## 5. 进程、资源、完整解码与视觉判定

将 `TASK_UNIT` 设置为本轮完整unit名、`TASK_OUT` 设置为对应新输出目录。以非敏感字段查询进度；不能用一次启动成功、`active/running` 或`evidence.json`已经存在当成完成。由于设置了 `RemainAfterExit=yes`，最终应为 `SubState=exited`、`Result=success`、`ExecMainStatus=0`，并同时核对证据和文件。

```bash
systemctl show "$TASK_UNIT" -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus -p ControlGroup -p Nice -p CPUQuotaPerSecUSec -p MemoryCurrent -p MemoryLimit -p TasksCurrent -p TasksMax
```

保存本轮journal及上述读回；在启动、中段、结束前记录本机v1的 `cpu.cfs_quota_us`、`cpu.cfs_period_us`、`cpu.stat`、`memory.limit_in_bytes`、`memory.usage_in_bytes`、`memory.max_usage_in_bytes`、`memory.failcnt`、`memory.oom_control`、`memory.memsw.limit_in_bytes`、`memory.memsw.usage_in_bytes`、`memory.memsw.failcnt`、`pids.current`、`pids.max`。memsw缺失须明确记录，不能省略后当作有保护。须结合该unit的 `ControlGroup`、实际进程的cgroup成员路径和mountinfo定位各控制器目录，正确扣除mount root，不能假设控制器同目录或读成其他服务。记录主机MemAvailable和同机FB是否自然开始制作；用短时、有限次的 `nvidia-smi dmon -s u -d 1 -c 10` 记录GPU编码/解码占用。只读观察，不发测试发布。

脚本兼容v2时会读 `cpu.max`、`memory.max`、`memory.swap.max`、`pids.max`，但本机的验收不能使用v2字段替代v1证据。若今后换为v2，内存事件证据应改用 `memory.events` 等对应文件并重新审查门禁。

脚本自动提供：

- `process-samples.jsonl`：每秒采FFmpeg子进程RSS、线程数、CPU时间/百分比，以及out_time/frame/speed；4核的CPU百分比可以超过100%。
- `evidence.json`：源SHA/大小、配方与素材指纹、配额读回、渲染器数、采样峰值、耗时和输出SHA；`peak_values_are_sampled=true`，采样峰值不等于内核绝对峰值。`limits.controllers`记录实际控制器目录，`limit_read_status`为complete/partial/unavailable，缺失或无效项在`read_errors`中明示；不能以字段缺失证明配额生效。
- CPU配额须同时有 `cpu_quota_read=true`、`cpu_quota_cores`为本轮预期2或4，以及对应v1 quota/period原始值；无上限时cores为null，不算通过。`ancestor_limits_checked=false`表明脚本没有检查上层限制：还需人工核对父cgroup是否有更紧限额，不能把该数值当作实际独占CPU或系统有效总配额。`limit_read_status=partial/unavailable`不得跳过缺失项验收，尤其不能忽略memsw。
- `renderer_elapsed_seconds`仅计渲染阶段；`elapsed_seconds`也不含脚本最初的ffprobe和源文件SHA预检。两者都不能声称是“下载到上传”的全流程耗时。

**脚本不会自动完整解码或视觉验收。** `full_decode_verified=false`、`visual_review_required=true`是工具的真实边界，不得手改成true冒充验收。渲染退出后，在没有其他benchmark运行时，用独立、同样低优先级的unit完整解码该成片；不产生新视频、不上传COS：

```bash
test "${TASK_SWAP_GUARD_CONFIRMED:-no}" = yes
systemd-run --unit="$TASK_UNIT-decode" --service-type=simple \
  --uid=drama-synthesis-gpu --gid=drama-synthesis-gpu \
  --property=RemainAfterExit=yes --property=KillMode=control-group --property=TimeoutStopSec=90 \
  --property=Nice=10 --property=CPUQuota=200% --property=TasksMax=128 --property=MemoryLimit=4G \
  --property=IOSchedulingClass=best-effort --property=IOSchedulingPriority=7 \
  /usr/bin/flock -n "$TASK_CONTROL/media-benchmark.lock" \
  "$TASK_FFMPEG" -nostdin -hide_banner -loglevel error -xerror -err_detect explode \
  -hwaccel none -threads 2 -i "$TASK_OUT/result.mp4" \
  -map 0:v:0 -map 0:a:0 -sn -dn -threads 2 -f null -
```

解码unit也必须在启动前落实独立交换保护，不能继承“渲染unit已保护”的假设。同样等待并读回decode unit最终状态与journal；`-xerror`非零、解码错误、缺少应有音频均失败。随后保存完整ffprobe JSON、重新计算输出SHA与证据对照。解码记录应另存 `decode-verification.json` 或验收报告，包含候选SHA、输出SHA、命令、开始/结束时间、退出码、错误数和证据路径，不篡改benchmark原始JSON。

| 检查 | 通过 / 停止规则 |
| --- | --- |
| 身份与编码 | 同一长/短样组内的源SHA、recipeSHA、素材manifest、二进制SHA一致；短源与长源各自冻结SHA，不要求二者相同。成片保持H.264 High、720×1280、yuv420p、30fps及原音频语义；不得删除模板、改变滤镜顺序或改编码质量来通过 |
| 时长与完整性 | 视频流、容器时长与各自源时长差均不超过既有0.15秒；完整解码退出0，无错误；成片SHA/大小匹配原始证据；仅ffprobe成功不够 |
| 真实渲染 | `ok=true`、`rendered_processes=1`、`sample_count>0`、实际子进程已退出；检查out_time/frame前进，不能因进度突然跳至尾部就认作所有帧正常 |
| 线程 | FFmpeg采样峰值建议不超过112，unit TasksCurrent保持低于120，TasksMax=128；没有fork/thread创建错误、触顶或持续增长。需越过保护值时停止评审，不调大TasksMax掩盖问题 |
| RSS保护 | 建议FFmpeg RSS峰值不超过12 GiB，v1 `memory.limit_in_bytes=17179869184`；独立memsw保护在进程启动前已核对。`memory.failcnt`与`memory.memsw.failcnt`无增长，`memory.oom_control`不处于OOM且journal无OOM/kill；同时核对usage/max_usage、交换占用和主机及FB余量。仅MemoryLimit或RSS低于16 GiB不能证明交换安全 |
| 长片RSS曲线 | 去除最初预热，按输出媒体进度25～50%、50～75%、75～100%分窗，计算RSS中位数；最后窗口相对前一窗口增长超过 `max(512 MiB, 前一窗口中位数×5%)`，或多个后段窗口持续增长未形成平台，均标记待查，不以“还没OOM”放行。该数值是保守评审门槛，需结合原始曲线与cgroup证据判断 |
| FB影响 | 不改变FB配置；FB自然负载出现时记录。发生资源争抢或明显延迟即停止本轮隔离unit并延期；不能用降低FB优先级换取drama提速 |
| 人工视觉/音频 | 播放开头含片头、首个剧集切换、中段至少两个集边界、结尾；检查字幕、横竖画面适配、随机色罩/边框/角标/动画循环、黑帧、重复/缺帧和口型/对白同步。保留截图时间点与验收人，不能用输出文件存在代替 |

停止资源异常的隔离测试时，只操作本轮已记录的完整unit名：`systemctl stop "$TASK_UNIT"`（解码则使用其精确decode unit名），然后确认其cgroup及子进程停止。不要使用通配符、`pkill ffmpeg`、删除检查点或操作旧正式任务。保留失败输出、prepared/start guard和日志；新试验换新目录。

## 6. 90分钟长片与配置决定

短样通过后，使用第2节同一保留长片，不裁剪、不循环短片，也不换配方。先为基线保留一轮长样记录，再对**实际拟采用**且短样通过的组合运行长样；每次先执行前述排空/资源门禁。发现明显持续内存增长时停止当前试验并查因，不盲目把所有组合跑完。

```bash
launch_media_case long-2c2t-r1 200% 2 long "$TASK_LONG"
```

该轮结束、完整解码/视觉/资源判定完成后，若拟采用4核2线程才运行下面一项；只有确有必要评估4线程且前述门禁仍满足时，另开全新的 `long-4c4t-r1`，不能把短样通过直接当成长样通过。

```bash
launch_media_case long-4c2t-r1 400% 2 long "$TASK_LONG"
```

采用4核4线程的命令形状为 `launch_media_case long-4c4t-r1 400% 4 long "$TASK_LONG"`。长样完成仍须执行第5节**整片完整解码**，并核对所有已知集边界时间点的异常；图层视觉抽查至少覆盖片头、早段、中段、后段和结尾。

比较相同输入下各轮 `renderer_elapsed_seconds` 的中位数、波动和资源曲线；性能差落在自然波动范围内就不能宣称提速。短样速度与长片速度分别报告，旧任务的混合下载/编码耗时不能混作纯渲染基线。资源/效果任一项未过，保持默认下载4、CPUQuota200%、滤镜线程2、主渲染并发1；CPU4或线程4只能在独立证据与确认后成为生产配置。

## 7. 交付记录与未执行项

每个样本的验收行须包含：候选GitHub SHA、批次和unit名、实际配额/Nice、源/配方/素材/运行时SHA、原始JSON与采样曲线、系统资源与FB观察、完整解码记录、人工视觉/音频结果、失败原因或配置决定。原始证据放在私有验收根；未经确认不上传到COS、不写正式任务完成状态、不自动接受用户验收。

当前状态：输入硬链接已保留；源SHA待计算，GPU配方副本待核对；候选 `570c1bdaba7eddb9cf881a5df7efee976c2d6fb0` 已在CPU/HK实际运行时各通过287项回归。**三组短样、90分钟长片、完整解码、RSS/线程判定全部未执行；TT媒体窗口、正式任务排空及本机v1交换保护门禁仍须满足。** 本手册未覆盖真实异步API故障/重启、下载并发/CDN、COS恢复和页面验收，这些仍按主测试报告分别完成。
