# 剧集合成媒体验收操作手册

更新：2026-08-31。**短片、CPU参数对照、完整解码及约90分钟长片验收均未执行；本文不是上线或性能通过证明。** 所有媒体动作必须使用同一精确GitHub候选内的 `scripts/run_drama_media_acceptance.py`；不得直接运行benchmark的render子命令或复制旧systemd模板。发布、备份和回滚见 [deploy.md](deploy.md)，实际结果写入 [test-report.md](test-report.md)。

## 1. 执行前提与当前阻塞

当前不能启动媒体。旧正式渲染已在2026-08-28约17:36:51自行退出，17:41核对没有视频成片；GPU服务未重启、没有OOM证据。退出时间与旧10800秒限时吻合，但原异常未保留，超时原因仍是证据支持的推断，见 [原任务证据](evidence/original-case-outcome-20260828.json)。没有终止旧任务，也不把CPU的failed状态改成done。

2026-08-31只读基线再次确认HK无ffmpeg/ffprobe、GPU空闲且剧集/FB服务未重启，见 [恢复基线](evidence/resume-baseline-20260831.json)；这是时间点快照，不是测试许可。远端窗口继续由迁移任务 `gpu-service-migration-20260828T1502` 协调；新候选dc0bad8的一次性CPU/HK无媒体隔离许可已执行完毕并关闭，结果见 [双端无媒体证据](evidence/linux-no-media-regression-dc0bad8-20260831.json)，仍未释放**本轮新的HK媒体/COS窗口**。旧候选1367dd4的许可、旧排空记录和旧窗口均不能复用，无媒体许可也不能升级使用。

启动验收必须同时满足：

1. 候选已审查、提交并推送GitHub，记录完整40位SHA；HK使用从GitHub取得的全新干净checkout。生产 `current`、正式unit和环境文件保持不变。
2. 与“统计GPU服务器运行任务”取得本轮书面窗口许可，并确认其共享目录、端口和GPU资源变更已停止。CPU公共目录/主API或HK剧集部署另行协调。
3. 通过已批准流程停止新的正式drama制作入口，并复核队列、租约、ffmpeg/ffprobe和GPU制作进程排空。页面空闲、旧PID退出或删除账本都不能替代排空。
4. 同机FB服务保持配置与优先级；不停止FB、不降低其配额，也不做真实FB发布测试。正式重制作并发仍为1。
5. `/data`空间、根文件系统、主机内存和固定输入重新核对。开始每个unit时 `MemAvailable >= 24 GiB`；可用内存低于8 GiB、FB受影响、OOM、任务触顶或未知进程出现时，停止本次精确unit并判未通过。
6. 一次只运行一个prepare/render/decode验收unit。固定锁只防这些验收动作互相并行；launcher**不占正式制作锁**，所以第3项不可省略。

已知HK为systemd `239-51.el8_5.2`、kernel `4.18.0-348.7.1.el8_5`、cgroup v1；执行前重查。launcher固定创建16GiB memory和memsw上限、swappiness0、TasksMax128、Nice10、NoNewPrivileges及最小降权能力，并在同一PID内写入后再次以非特权身份读回。它使用 `/usr/bin/nice -n 10`；**禁止再用已被现场证明可能得到实际Nice0的 `--property=Nice=10` 旧模板。** 缺失memsw、父级余量不足、身份/能力/锁/进程回收不明均失败关闭，不能用手工确认变量或放宽限制继续。

## 2. 固定输入与不可变身份

| 项目 | 固定记录 / 执行前动作 |
| --- | --- |
| 长片硬链接 | `/data/drama-synthesis-gpu/acceptance/20260828-reliability/inputs/case-679e7c49-concat.mp4`；大小 `5139047136` 字节 |
| 原concat路径 | `/data/drama-synthesis-gpu/work/jobs/679e7c49acbf4af79f78bf60d76c5dd7/8HehaA3263_679e7c49_eps_1_70.mp4`；可能已被正常清理，验收只使用保留硬链接 |
| 源SHA256 | **尚未计算**；取得窗口后由launcher在受控unit内完整读取并写入本批次私有 `long-source.json`。同一批次所有prepare/long-render都必须逐字复核该SHA、大小、device、inode、mtime和nlink；不能只用大小或inode代替，也不能为另一配置或轮次重新接受新内容 |
| 长片时长 | 旧记录约 `5573.906333` 秒；执行前重新ffprobe，必须在5400～7200秒 |
| 配方 | 固定路径 `.../inputs/case-679e7c49-recipe.json`；规范化指纹 `56d60ff057e0da8eb08b0ef8063be0ef75d37d28a970c1c912ad915f8de9793f` |
| profile | `drama-random-overlay-h264-720x1280-v1`，`source=concat_video` |
| 素材根 | `/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114` |
| 素材manifest | `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f` |
| ffmpeg/ffprobe | 固定HK运行时路径 `/data/drama-synthesis-gpu/runtime/bin/ffmpeg` 与 `.../ffprobe`；执行前记录realpath、版本和SHA |

**不得对长片硬链接执行chmod、chown、截断、重写或“修复”，也不得递归修改输入目录。** launcher只读全局长片、配方和素材。短源由受控prepare动作从固定长片stream-copy到本次0700私有run root，先写O_EXCL `.part`，fsync、ffprobe和SHA校验后同目录无覆盖原子提交；不会开放全局inputs写权限。

所有短片配置和两轮试验必须读取同一个prepared短源及其证据。长片不裁剪、不循环短片、不重新编码源；保留片头、集序、25fps标准化语义、30fps模板、NVENC/profile和滤镜顺序。

`long-source.json` 是一次批次级身份记录，不是可编辑的操作参数。首次prepare或long-render会在完整哈希通过后以0400权限、无覆盖方式写入；已存在但损坏、字段不全或与当前长源不一致时必须停止。prepare完成、每次long-render完成后还要重新完整复核；不得删除该记录来接受变化后的同名文件。

## 3. 候选、16GiB无媒体门禁与短源准备

以下命令只展示固定调用形状，当前未执行。尖括号必须换成本轮已推送候选和新的8～30位小写批次标识；同一轮短片、长片和解码使用同一个批次。先在候选目录执行 `--help`，并核对candidate SHA及工作区全清洁。

```bash
TASK_SHA='<REVIEWED_40_HEX_GITHUB_SHA>'
TASK_RUN='<new-lowercase-run-id>'
TASK_CODE="/data/drama-synthesis-gpu/acceptance/code/$TASK_SHA"
TASK_PYTHON=/data/drama-synthesis-gpu/runtime/current/bin/python
TASK_LAUNCHER="$TASK_CODE/scripts/run_drama_media_acceptance.py"
TASK_LAUNCHER_SHA256='<REVIEWED_LAUNCHER_SHA256_FROM_MANIFEST>'

TASK_ACTUAL_LAUNCHER_SHA256="$(/usr/bin/sha256sum -- "$TASK_LAUNCHER")" || exit 78
test "${TASK_ACTUAL_LAUNCHER_SHA256%% *}" = "$TASK_LAUNCHER_SHA256" || exit 78
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --help || exit 78
```

上面的人工命令只核对已评审manifest中的launcher字节，不能代替launcher自身门禁；不要运行 `git status`，仓库内attributes/filter配置可能让它执行外部转换。launcher固定使用root拥有且不可组/其他写的 `/usr/bin/git`，显式绑定当前work-tree，设置 `GIT_NO_LAZY_FETCH=1`，屏蔽system/global config、replace refs、hooks和untracked cache；fsmonitor统一用兼容Git2.27的空值覆盖，并在首次读取index前确认可见配置只有该command-line空值，任何local/worktree/include值都拒绝。它用HEAD tracked清单与实际常规文件集合、实际目录集合精确比较，因而拒绝未跟踪、已忽略文件（包括 `__pycache__/*.pyc`）、assume-unchanged、skip-worktree、fsmonitor-valid、symlink/submodule及非精确HEAD；再逐个以 `hash-object --no-filters` 核对整个HEAD工作树。候选路径祖先、递归目录、全部tracked文件、`.git`入口和实际gitdir都必须root-owned且不可组/其他写；执行位须与Git mode一致。导入前后还会复核完整文件身份，不能被 `core.worktree`、clean filter或stat cache转向/隐藏。任一项失败均不得导入候选模块或提交unit。公开、guard与verified阶段统一以 `-I -S -B` 运行，不执行site、`.pth`或候选外sitecustomize；资源guard脚本的standalone CLI仅为launcher内部入口，操作员不得直接调用，它本身没有也不需要公开 `--apply`。

先运行不读取媒体、不取媒体锁、不创建run root的16GiB guard-only。缺省命令只预览；核对JSON的 `operation=guard-only`、`media_started=false`、`ffmpeg_processes=0` 和 `ffprobe_processes=0` 后，取得迁移任务对该轻量unit的许可，再增加 `--apply`：

```bash
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --guard-only \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind short --config 2c2t --trial r1

"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --guard-only \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind short --config 2c2t --trial r1
```

提交成功只表示systemd接受unit。等待精确 `drama-media-guard-<sha12>-<run>.service` 结束，保存非敏感journal和unit状态，核对8MiB/3秒普通probe、16GiB memory/memsw、swappiness0、CPU2、Tasks128、实际Nice10、uid/gid、groups空、CapEff0、NoNewPrivileges1；媒体进程必须为0。先前256MiB自检是两次正向成功加一次预期负向拒绝，不能代替本轮16GiB读回。

launcher在调用 `systemd-run` 前先持久化提交意图，确认systemd接受后再写accepted记录。若客户端超时、被中断或返回 `completion_unknown=true`，`media_started` 必须视为未知；**不得以同一批次重试，也不得删除intent记录**。先按返回的完整unit名只读执行 `systemctl show` 与 `journalctl -u`，区分正在运行、已结束、提交结果仍未知；在原unit、全部子进程、检查点和产物完成唯一对账前，任何新批次也不得重放该动作。网络或终端响应丢失不等于unit未创建。

媒体窗口释放、正式入口排空且固定配方到位后，prepare同样先预览，再用 `--preflight` 做只读检查，最后才 `--apply`：

```bash
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --preflight --prepare-short \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind short --config 2c2t --trial r1

"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --prepare-short \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind short --config 2c2t --trial r1
```

等待精确prepare unit自然结束。核对 `prepared-short.json`、短源0400权限、115～125秒时长、音视频流、SHA/大小和全局长源身份。prepare使用一次受控ffmpeg stream-copy及一次ffprobe，`media_started=true`；它不是模板渲染，也不能被写成渲染性能样本。失败或响应丢失时保留私有run root和`.part`，不覆盖重跑同批次。

## 4. 两轮三配置短片渲染

默认操作是render；每次缺省仍只预览，实际执行必须显式 `--apply`。第一轮按2c2t→4c2t→4c4t，第二轮倒序。前一unit结束、证据保存并完成第5节decode后，才能运行下一项。

```bash
# r1
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" --sample-kind short --config 2c2t --trial r1
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" --sample-kind short --config 4c2t --trial r1
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" --sample-kind short --config 4c4t --trial r1

# r2，倒序
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" --sample-kind short --config 4c4t --trial r2
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" --sample-kind short --config 4c2t --trial r2
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" --sample-kind short --config 2c2t --trial r2
```

每个 `sample/config/trial` 绑定不同unit、输出目录、launcher guard/result和decode证据；同名重放必须拒绝，不能删除目录换取重跑。所有组合校验同一个prepare SHA。launcher持有固定锁并把同一FD传给新renderer，启动后立即用 `/proc/<pid>/fd` 验证继承；继承或清理/reap不明时必须显式失败，不能声称子进程已停止。

## 5. 完整解码、资源和效果判定

每个render完成后，用相同sample/config/trial调用固定decode动作。它只读取已由launcher及benchmark证据绑定的 `result.mp4`，在新的16GiB受控unit内执行固定 `ffmpeg -xerror ... -f null -`，不生成新视频、不上传COS：

```bash
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --decode \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind short --config 2c2t --trial r1
```

对其余组合只替换固定的config/trial枚举。decode证据必须为新文件并绑定render unit、decode unit、成片SHA/大小、显式退出码0和持续时间；完整解码前后都要重新读取并核对成片SHA及文件身份。非零、缺少应有音频、SHA/identity变化或cleanup不明均失败。不要用手写systemd-run、任意路径ffmpeg或已存在decode证据替代。

保存每个精确unit的 `systemctl show` 非敏感字段和journal；结合launcher guard、benchmark `evidence.json`、`process-samples.jsonl`、decode证据、主机MemAvailable及FB自然负载判定。不得输出环境文件、Token、源URL、COS凭据或完整进程命令。

| 检查 | 通过 / 停止规则 |
| --- | --- |
| 身份与编码 | 候选SHA、长/短源SHA、recipeSHA、素材manifest和二进制SHA冻结；实际片头封面必须通过JFIF/sRGB合同并验证BT.709 limited输出，若为PNG/WebP/ICC/Adobe JPEG则停止而非跳过片头；标准化段须证明等比scale+pad、显式场序转换及短/缺音轨不截视频；最终成片保持H.264 High、720×1280、yuv420p、30fps及原音频语义，不删模板或改变滤镜/编码质量 |
| 时长与完整性 | 视频流、容器时长与源时长差均不超过既有0.15秒；完整decode退出0且成片SHA/大小不变；ffprobe成功不能代替完整解码 |
| 真实渲染 | `ok=true`、`rendered_processes=1`、`sample_count>0`、实际子进程已reap；out_time/frame持续前进，输出文件存在不等于通过 |
| 线程 | FFmpeg采样峰值建议不超过112，保护阈值为大于120立即停止；TasksMax128，不用提高上限掩盖增长 |
| RSS与cgroup | 建议FFmpeg RSS峰值不超过12GiB，采样达到14GiB立即停止；memory与memsw均为17179869184、swappiness0，主机可用内存不低于8GiB。每个prepare/render/decode必须在同一cgroup内记录动作前后 `memory.failcnt`、`memory.memsw.failcnt`、`total_swap`、`under_oom` 和 `oom_kill`；计数增长、swap非零、压力证据缺失或cgroup身份变化均失败 |
| 长片RSS曲线 | 去除预热后按媒体进度25～50%、50～75%、75～100%分窗计算RSS中位数；最后窗相对前窗增长超过 `max(512MiB, 5%)` 或后段持续增长未形成平台，标记待查，不以“未OOM”放行 |
| FB影响 | 不改FB配置；FB自然负载出现时记录。争抢或明显延迟即停止本次精确unit并延期，不降低FB优先级换取提速 |
| 人工视觉/音频 | 播放片头、首个切集、中段至少两个集边界及结尾；检查字幕、画面适配、色罩/边框/角标/动画、黑帧、重复/缺帧和音画同步。记录时间点和验收人 |

停止异常试验只使用本轮完整unit名并确认其cgroup和子进程全部停止。不得用通配符、`pkill ffmpeg`、重启生产服务、删检查点或操作原正式任务。失败输出、提交意图、start/prepared记录和日志全部保留；修复后使用新批次。

下面是short/2c2t/r1 render的完整停止示例。`TASK_SUBMIT_JSON` 必须是该次apply返回并原样保存的单行JSON；从其中提取精确unit，禁止按SHA/run/config手拼：

```bash
set -eu
TASK_SUBMIT_JSON='<EXACT_SINGLE_LINE_JSON_RETURNED_BY_THE_APPLY_COMMAND>'
TASK_UNIT="$(printf '%s\n' "$TASK_SUBMIT_JSON" | "$TASK_PYTHON" -I -S -B -c 'import json,sys; value=json.load(sys.stdin); sha,run=sys.argv[1:]; unit=value["unit"]; expected="drama-media-accept-%s-%s-short-2c2t-r1.service"%(sha[:12],run); identity=value.get("candidate_sha")==sha and value.get("run_id")==run and value.get("operation")=="render" and value.get("sample_kind")=="short" and value.get("configuration")=="2c2t" and value.get("trial")=="r1" and unit==expected; accepted=value.get("ok") is True and value.get("submitted") is True and value.get("completion_unknown") is False; unknown=value.get("ok") is False and value.get("completion_unknown") is True and value.get("replay_forbidden") is True; assert identity and (accepted or unknown); print(unit)' "$TASK_SHA" "$TASK_RUN")" || exit 78
case "$TASK_UNIT" in drama-media-accept-*.service) ;; *) echo "unexpected unit" >&2; exit 78 ;; esac
test "$(systemctl show "$TASK_UNIT" --property=Id --value)" = "$TASK_UNIT" || exit 78
systemctl show "$TASK_UNIT" --property=LoadState,ActiveState,SubState,Result,ExecMainPID,ControlGroup || exit 78
TASK_CGROUP="$(systemctl show "$TASK_UNIT" --property=ControlGroup --value)" || exit 78
test "$TASK_CGROUP" = "/system.slice/$TASK_UNIT" || exit 78
TASK_CGROUP_PATHS=""
for TASK_CONTROLLER in /sys/fs/cgroup/memory /sys/fs/cgroup/pids /sys/fs/cgroup/cpu,cpuacct /sys/fs/cgroup; do
  TASK_PATH="${TASK_CONTROLLER}${TASK_CGROUP}"
  if test -d "$TASK_PATH"; then
    TASK_CGROUP_PATHS="$TASK_CGROUP_PATHS $TASK_PATH"
  fi
done
test -n "$TASK_CGROUP_PATHS" || exit 78
systemctl stop -- "$TASK_UNIT" || exit 78
case "$(systemctl is-active "$TASK_UNIT" 2>/dev/null || true)" in
  inactive|failed) ;;
  *) echo "exact unit is still active" >&2; exit 78 ;;
esac
for TASK_PATH in $TASK_CGROUP_PATHS; do
  if test -d "$TASK_PATH"; then
    TASK_PROCS="$(cat "$TASK_PATH/cgroup.procs")" || exit 78
    test -z "$TASK_PROCS" || exit 78
    if test -e "$TASK_PATH/tasks"; then
      TASK_THREADS="$(cat "$TASK_PATH/tasks")" || exit 78
      test -z "$TASK_THREADS" || exit 78
    fi
  fi
done
```

保留停止前后的 `systemctl show` 和journal；确认精确unit不活跃、cgroup没有成员后才能判定子进程已清理。若controller路径与本轮launcher证据不一致，停止并人工核查，不能据此操作相邻cgroup。

## 6. 约90分钟长片与参数决定

六个短片render和decode均通过后，先运行长片2c2t/r1基线，再只对短片通过且拟采用的配置运行长片。每轮仍先预览/`--preflight`、重查窗口和排空，再显式apply；示例：

```bash
"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind long --config 2c2t --trial r1

"$TASK_PYTHON" -I -S -B "$TASK_LAUNCHER" --apply --decode \
  --candidate-sha "$TASK_SHA" --run-id "$TASK_RUN" \
  --sample-kind long --config 2c2t --trial r1
```

若拟采用4c2t，则运行并完整解码long/4c2t/r1；4c4t只有在短片证据显示必要时才运行。发现持续内存增长、FB影响、线程压力或效果问题立即停止当前精确unit，不盲目跑完全部配置。

比较相同输入下两轮短片的中位数、波动及长片 `renderer_elapsed_seconds` 和资源曲线。CPU参数只采用通过长片完整解码、视觉/音频和资源门禁且耗时最短的组合；相对2c2t收益不足15%则保留2核/滤镜2。无论结果如何，重制作并发保持1。短片速度、长片速度和旧任务的下载/标准化耗时分开报告。

## 7. 交付记录与当前未执行项

每个样本记录：候选GitHub SHA、批次、operation/unit、实际配额/Nice/memsw/Tasks/身份、源/配方/素材/运行时SHA、launcher与benchmark原始JSON、进程曲线、完整解码、FB观察、人工视觉/音频结果，以及失败原因或参数决定。原始证据保存在私有验收根；未经确认不上传COS、不回填正式任务、不代替用户接受。

当前状态：长片硬链接已保留，源SHA待计算，GPU固定配方副本待窗口内核对；媒体launcher已作为dc0bad8候选推送，CPU/HK目标Linux无媒体回归已通过。**16GiB guard-only、短源prepare、六组短片render/decode、约90分钟长片、完整解码及人工效果验收均未执行；新的HK媒体/COS窗口也未释放，不能沿用无媒体许可。** 本手册不替代异步API重启/故障、下载1/2/4/8、真实COS恢复或页面业务验收，最终状态以 [test-report.md](test-report.md) 为准。
