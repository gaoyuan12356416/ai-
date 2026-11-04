# SA 代码评审

## 结论

SHA 的独立 GPU 故障回归 7/7 通过；MP4/Annex B 独立 GPU 回归最终 14/14 通过。追加 Annex B 转换曾发现 GPU 越界（BUG-007），已修复并复验。此前 SHA 异常复用、进程归属、跨流初始化及 MP4 常驻 payload/临时文件问题均已修复。此结论仍不构成全制作管线验收或可部署结论。

## 评审范围

- `features/random_gpu/gpu_compute.py`：标准顺序 SHA-256、状态提交、并发/跨流/进程归属、文件分块。
- `features/random_gpu/gpu_mux.py`：时间基、交织、ctts/stts/stco/co64、box 序列化、文件提交与取消；Annex B 转换为本轮追加审查项。
- 独立测试只使用隔离 venv 和微型输入，没有修改生产配置、暂停任务或重启服务。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 / BUG-001 | P1 | CudaSHA256.update/hexdigest | 分配失败后可重用错误状态，或 final 已完成但报错后重试二次 padding，返回错误摘要 | 状态计算成功后提交；GPU 异常后对象永久失败 | 已修复，故障注入回归通过 |
| CR-02 / BUG-002 | P1 | cupy_runtime/CudaSHA256 | fork 后可继承父进程 context/cache 与 digest 锁状态 | CUDA runtime/digest 记录 PID，在 CUDA 调用和取锁前拒绝子进程继承 | 已修复，模拟 PID 变化回归通过；未在生产 fork |
| CR-03 / BUG-003 | P1 | CudaSHA256.__init__ | nonblocking stream 异步 state 初始化后，在另一 stream 首次更新可读到未完成的状态 | 构造器同步所属初始化 stream，或显式建立 event 依赖 | 已修复；原来稳定失败的真实 GPU 跨流用例通过 |
| CR-04 / BUG-004 | P2 | CudaMP4 Sample/文件输入 | 接口仅接受常驻 bytes，整片压缩数据乘并发数无界占用主机内存 | 支持磁盘 PacketFile 引用；生产调用实际使用 spool | 已修复接口，PacketFile/bytes 等价通过；并发 RSS 仍需整体验证 |
| CR-05 / BUG-005 | P1 | CudaMP4 临时文件清理 | 固定名字的 exists/open(xb) 竞争失败后 finally 删除另一 writer 文件 | mkstemp 创建唯一且归属明确的临时文件 | 已修复；取消/截断保留其他临时文件回归通过 |
| CR-06 / BUG-006 | P2 | avcc_config | High profile 配置缺少 chroma/bit-depth 等扩展字段 | 从 SPS 前缀解析并生成完整字段 | 已修复，High/SPS/EPB/截断回归通过 |
| CR-07 / BUG-007 | P1 | annexb_sample | uint64/int64 混合计算将 dest 转 float64，kernel 按 uint64 解读导致越界 | 全部 offset 运算显式 uint64，核对 RawKernel ABI | 已修复，原故障用例回归通过 |
| CR-08 / BUG-008 | P2 | MP4 track/movie时长 | 所有轨道沿用请求时长，短片音视频实际结束不同 | 每轨展示结束及movie最大结束分别计算 | 已修复，3组真实短时长通过 |
| CR-09 / BUG-009 | P2 | mux验证器 | seek(0)漏取极短AAC负PTS priming包 | 每轨重新打开，从头demux | 已修复，极短2包及2秒155包复验 |
| CR-10 / BUG-010 | P2 | native_pipeline/run_render | kill时mux临时文件可能留在父scratch外 | scratch内mux，成功后提升最终输出 | 路径修复，父清理控制测试通过 |
| CR-11 / BUG-011 | P2 | _measure_processor | native Python渲染误标probe | 模块命令识别为render | 已修复，正常/异常恢复2项通过 |
| CR-12 / BUG-012 | P1 | X非零预算+CQ NVENC构造 | SDK CQ分支静默清空平均/最大码率；重配getter仅构造缓存 | 先无CQ构造读回预算，再应用保留预算的CQ重配并明确metadata | 已修复；3组配置、4组复杂画面、2组X完整短片验证 |
| CR-13 / BUG-013 | P2 | HEVC参数集长度 | 只有2byte NAL header的空PPS被接受 | 参数集至少包含RBSP字节 | 已修复；新增9项HEVC/ESDS独立GPU回归通过 |
| CR-14 / BUG-014 | P2 | AAC esds码率 | 固定192k错误描述X128k音轨 | CUDA计算实际平均，最大未知写0 | 已修复；独立解析esds数值通过 |

另外，极端绝对时间戳的 int64 缩放及相减仍需与调用者的输入边界一起核对；不能把常规短片通过视为任意 64 位时间戳正确的证明。未发现正常业务长度会触及该边界的证据。

## 编译 / 验证结果

2026-11-04，隔离目录 `/data/pipeline-dispatch/evaluations/gpu-full-offload-20261104/compute-code`，解释器为同级 `venv/bin/python`：

```text
python compute-code/scripts/test_gpu_compute_failures.py
Ran 7 tests in 0.964s — OK
python compute-code/scripts/test_gpu_mux_boundaries.py
Ran 14 tests in 4.832s — OK（最新按实际轨道时长修复后）
```

SHA 覆盖分配失败、final 未知完成状态、继承进程拒绝、跨 nonblocking stream、多个对象/线程、空文件与奇数 chunk。标准向量等另由根代理 `validate_gpu_compute.py` 验证，不能以故障测试代替向量校验。

MP4 覆盖视频单轨、双轨负 AAC DTS/相同 DTS 稳定交织、ctts v1 负数、duration 无效值/uint32 上界、gap 与正 decode origin、PacketFile 完整/截断、取消与旧输出保留。co64 不仅验证表值，还用逻辑 4 GiB payload 触发自动重建，在写 payload 前终止，核对实际 header 长度与两个最终 offsets；没有实际写入 4 GiB。

合成 packet 只用于容器结构验证，不证明 H264/AAC 能解码。QA已独立复跑真实 B 帧+AAC 155 packet 重封装，包内容/时间戳和完整音视频解码哈希等价（qa-mux-results.json）。全模板与长片并发、恢复和生产集成仍待完整测试报告。

追加 5 项 Annex B/avcC 测试包括混合 3/4-byte start code、EPB payload 保留、IDR key 判别、错误/空 NAL、High SPS Exp-Golomb/chroma/depth/EPB、截断拒绝及真实 NVENC SPS。独立解析真实源/候选 `mdhd` 也相同：视频 time scale15360、duration30720；音频48000、97024。FFmpeg n7.1 `calc_samples_pts_duration` 采用末 PTS 结束减首 PTS（首 DTS+首 CTS），不是减首 DTS；当前 CFR B 帧输入 sum(duration) 与此相等，不能把 decode delay 再加到 mdhd。

本地 `python -m py_compile scripts/test_gpu_compute_failures.py scripts/test_gpu_mux_boundaries.py` 与 `git diff --check` 通过。

新增安装器gpu_native/verify-local共16项mocked测试通过；native命令度量2项通过；父进程watchdog控制5项通过。安装器的代码修改由QA执行，需由根代理复审；这些mocked测试不能代替新SHA干净venv的真实stage/doctor。

进程模型核查：run_node使用spawn，不会继承父CUDA context。capabilities构造TT配置仍会在supervisor触发catalog GPU hash并保留额外context，应计入显存成本，未据此虚构fork故障。

最新完整native管线30秒真实样本的原音频时间戳洞失败已兼容修复并由根代理重测通过。X适配保留原output_rate_control/128k AAC/CPU profile/验收和冻结recipe，native stall不经过旧归一化修改内容；22项本地回归、3种实际码率配置及2组完整短片通过。随后新增源格式/HEVC/TT仍需独立验收，最终SHA不能只沿用中间版本报告。共享文件hash接入不等于所有视频流程已迁移。

用户要求若部署需暂停任务，应先告知并由用户暂停确认。上述测试不包含任务暂停、drain、服务 restart 或 release switch。

HEVC独立评审：PTL位置、96bit总字段、子层profile88bit跳过、Exp-Golomb/conformance window及EPB恢复与单层HEVC结构相符。VPS/SPS层数合并和多层拒绝已覆盖。最新9/9（2.928秒）及H26414/14（6.454秒）通过；重新生成12帧HEVC，CUDA mux前后完整解码SHA相同。与FFmpeg对同raw的hvc1参考相比，23byte配置头、VPS/SPS均逐字节相同；参考PPS多1个尾零，未谎称整个extradata相同。证据`hevc-mux/qa-header-comparison.json`和`qa-hevc-mux/result.json`。当前约束是单层编码器单VPS/SPS/PPS，未泛化任意多层/中途改配置。

## 追加：实际运行漂移的部署阻塞

QA已只读核实生产实际运行`/data/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/node`，来源是clean Git f3bd38531f6da7e11fbd853444b7d12d95f50ff2的五文件FB租约补丁，非裸179596a。原1,736 tracked files无缺失，仅四既有文件改变加job_lock.py；五文件SHA均与runtime manifest/source相同。具体证据、权限及unit结构见deploy.md。源分支全树与179596a不同，不能全树替换丢失既有X修复。

上线前必须解决：

- 候选保留排他job锁、下载/渲染lease检查、lease异常透传、输出时长和CPU线程约束；与native进程组kill/scratch清理一起回归。不能将旧render_process整文件覆盖本次watchdog。
- 实际worker SHA为25ccd40ac0b3693a2d8bfad6d8edc7f1980ca968e4bcc16a58fbcac85353273c，仅新增check_render，signal/drain本身未变；仍需审后更新白名单。
- 现helper的clean-release/argv检查无法接受当前overlay；99-fb-lease-receipt-20261104.conf会覆盖新base unit ExecStart。必须受测地处理exact drop-in及真实runtime身份，不能绕过检查。回滚须恢复此lease overlay。
- installer.activate只检查is-active返回码，不能排除deactivating/restarting；激活前必须明确inactive/MainPID0、无残留cgroup进程/待执行job，或实现相应校验。

这些是当前部署阻塞，不是GPU组件测试失败。QA未执行任何服务控制或生产文件变更。

后续独立复核：五文件融合保留native进程组/scratch清理、每poll租约检查、下载execution_check、lease异常透传和输出时长校验，worker新增check_render未改变原signal/drain。QA本地复跑专用切换脚本19项、节点lease5项、native控制8项：共30项，29通过、1个Linux进程组专项按平台跳过；根代理另报Linux隔离13项全部通过。新脚本由QA实现、根代理复审；已根据根代理反馈修复“撤99后reload前失败”的cached unit恢复边界并故障注入验证。今天按用户安排仅开发收尾，未做正式环境验收或部署。
