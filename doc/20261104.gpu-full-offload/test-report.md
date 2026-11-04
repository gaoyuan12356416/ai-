# 测试报告

## 测试结论

**尚未完成最终版本验收，尚未部署。** 独立SHA、MP4/Annex B、安装器及watchdog组件测试通过；原30秒真实native全链音频时间戳洞已修复并重测通过。X默认及受限大小预算两组真实两秒成片通过。TT及新增源格式/HEVC兼容仍在集成，最终GitHub SHA需重核，不将中间版本的结果冒充最终发行验收。

完整GPU AAC编码候选未通过质量验收，压缩音频解码仍在CPU。当前可测试增量路径是CPU解码 → CUDA DSP → 已有native CPU AAC → CUDA MP4；这一事实必须明确，不能称作“所有能迁移的计算已全部迁移完成”。

## 测试范围

| 流程 | 当前结果/边界 | 上线就绪 |
| --- | --- | --- |
| 文件SHA-256 CUDA实现 | 标准向量由根代理验证，QA故障/跨流/状态隔离7项通过；共享文件hash调用点已接入 | 组件通过，需最终路由回归 |
| NVDEC/CUDA/NVENC | FB H264 High/CQ21 30秒全链通过；X H264/CQ21受限码率和128k AAC两组2秒通过 | TT及新增兼容范围待整体验收；非零预算+CQ需BUG-012修复 |
| CUDA音频DSP+CPU原生AAC | 音频代理128k扩展15项、192k扩展14项通过，含5.1/时间戳洞兼容；真实30秒音频PCM同基线 | 连续支持输入走CUDA DSP；不支持布局/时间洞保留明确CPU兼容，不虚称全GPU |
| 完整GPU AAC | 已证实DSP、部分编码计算、bitpack/ADTS可GPU；完整候选质量未通过 | 未就绪，不列为已迁移 |
| CUDA MP4/Annex B | H264 14项、HEVC/ESDS 9项、3组短时长、真实H264/AAC及12帧HEVC完整解码等价通过 | 组件通过，仍需全链长片/并发 |
| GDS | 当前virtio_blk/ext4、无cuFile/nvidia-fs/RDMA/P2PDMA通路 | 当前不可直接启用；未做端到端直传 |
| 安装/生命周期 | 本地16项安装器、2项度量；Linux watchdog6项含孙进程kill/清理通过 | 新SHA干净venv doctor/COS/激活尚未执行 |

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| QA真实GPU SHA故障/跨流 | 7 | 7 | 0 | 0 |
| QA真实GPU mux/Annex B边界（最新） | 14 | 14 | 0 | 0 |
| QA实际FFmpeg短时长→GPU重封装 | 3 | 3 | 0 | 0 |
| QA真实2秒B帧+AAC包/全解码等价 | 1 | 1 | 0 | 0 |
| QA本地安装器mocked | 16 | 16 | 0 | 0 |
| QA阶段度量正常/异常恢复 | 2 | 2 | 0 | 0 |
| 根代理Linux子进程watchdog | 6 | 6 | 0 | 0 |
| 音频代理128k扩展原生AAC/DSP集成 | 15 | 15 | 0 | 0 |
| 音频代理192k扩展原生AAC/DSP集成 | 14 | 14 | 0 | 0 |
| 根代理30秒native全链最新尝试 | 1 | 1 | 0 | 0 |
| QA X适配本地新增/原有回归 | 22 | 22 | 0 | 0 |
| QA X三种预算实际配置 | 3 | 3 | 0 | 0 |
| QA X真实2秒默认/小预算完整链 | 2 | 2 | 0 | 0 |
| QA HEVC/ESDS GPU边界 | 9 | 9 | 0 | 0 |
| QA 12帧HEVC重封装解码等价 | 1 | 1 | 0 | 0 |

不把历史中间版本的重复回归相加为“最终通过数”。上表组件版本由对应最后测试时工作树确定；正式交付仍需记录最终GitHub SHA下的验收。

## 缺陷情况

BUG-001–014分别记录已确认问题、修复和证据。SHA状态/跨流、临时文件所有权、High avcC、Annex B类型越界、轨道时长、priming取包、scratch清理及阶段度量已修复；无界payload问题已改PacketFile接口，实际长片并发RSS仍需测试。BUG-012实测确认SDK的CQ初始化清空码率预算，已用保留码率的重配API修复并验证；BUG-013空HEVC PPS和BUG-014固定AAC esds码率已修复。时间戳洞兼容已在原失败夹具通过。

## 验证证据

隔离目录：`/data/pipeline-dispatch/evaluations/gpu-full-offload-20261104`；没有在生产任务上执行故障注入或重置设备。

- `compute-code/scripts/test_gpu_compute_failures.py`：7/7，0.964秒。
- `compute-code/scripts/test_gpu_mux_boundaries.py`：最新14/14，4.832秒；包括逻辑4GiB自动co64重建，未实际写4GiB。
- `qa-duration-gpu.json`：0.01/0.10001/1.00001秒；分别验证每轨展示结束和movie结束。极短AAC完整2包，首DTS=-1024。
- `qa-mux-results.json`：155包，包hash/PTS/DTS/duration/key完全相同；视频解码6,912,000 bytes、音频770,048 bytes均SHA相同，status=passed。
- `audio-results/audio-track-expanded.json`（音频代理）：12项。48k主要场景PCM逐字节等价；44.1/16/96k以独立PCM参考评估。SNR差不低于−0.5dB仅为本次数值准则，不是全内容主观听测证明。
- 本地 `python -m unittest scripts.test_pipeline_node_install -v`、两个native度量用例、`scripts.test_native_pipeline_control`通过；相关py_compile与git diff --check通过。
- `audio-results/audio-track-compat-expanded-128k.json`、192k对应报告（音频代理）：15/14项；`real-30s-compat.json`为原失败真实源，1408 AAC包、有效1440000样本、容器30.000秒，PCM同当前FFmpeg。CPU兼容分支仅报告真实PCM字节推进，CUDA异常不降级掩盖。
- `real-fixture/native-integrated30-final.acceptance.json`（根代理）：900帧/1408音频包/30秒、严格全解码通过；CPU15.5785秒、墙钟30.016秒、GPU峰值555MiB、RSS395856KiB。相同夹具旧版CPU52.867秒/墙钟67.391秒/GPU704MiB/RSS662200KiB。这是单任务测量，不是整台机器CPU百分比或并发容量证明。
- `qa-x-native-rates-fixed.json`：SDK2.2.3实际初始化码率与保留CQ重配，30/1200/14400秒预算3项通过；`qa-x-rate-content.json`为同画面码率/CQ行为验证，详见BUG-012。
- `real-fixture/qa-x-native2.acceptance.json`、`qa-x-native2-smallbudget.acceptance.json`：两组各60帧/95AAC/2秒，H264 High/YUV420p/720x1280/30fps/AAC LC 48k stereo，128k音频；普通大小1498012B、小512KiB上限下435292B，原X验收与严格全解码通过。
- 最新H264边界14/14（6.454秒），新增HEVC/ESDS边界9/9（2.928秒）；`qa-hevc-mux/result.json` 12帧重封装前后完整解码1,105,920 bytes、SHA相同。`hevc-mux/qa-header-comparison.json` 独立FFmpeg参考的23byte头及VPS/SPS完全相同，PPS参考多1个尾零。
- `native30-comparison.json`（根代理）：30秒完整音频PCM逐样本SHA同当前流程，900帧视频SSIM=0.992014；5秒截图人工复核未见几何/色彩/字幕/图层位置偏移。这是该夹具证据，不代表任意源格式的主观质量证明。
- 401MB真实整文件（根代理）：CUDA SHA=hashlib=冻结源SHA。GPU冷启动32.13秒墙钟/2.075秒CPU，对照CPU SHA2.052秒墙钟/1.746秒CPU。GPU哈希在该单文件上明显增加墙钟成本，并未降低CPU时间；按用户全部可迁移计算的授权保留GPU路径，但不得把制作渲染的性能改善推广到哈希。

短片AAC解码器可能输出完整1024-sample帧：例如0.01秒有效480样本、解码1024样本；必须分别报告编码帧padding与容器有效展示时长，不把它们混为相同数字。

## 遗留风险

- 30秒真实源已通过，但此后还有新增解码兼容/HEVC变更，最终SHA应重跑代表性完整链，不能直接沿用中间版本签收。
- X两组短片及预算配置通过，长片/边界大小仍待最终验收；TT的preset/AQ/lookahead/HEVC合约集成仍在进行，不允许复用FB配置改变内容。
- 新SHA clean venv的PyNvVideoCodec2.2.3/NVRTC12.6.85组合必须实际doctor；早期实验venv/覆盖目录不是生产环境证明。
- 仍需实际长片/并发资源、native全链画面质量与AV同步、取消恢复/产物/回执边界验证。supervisor的catalog校验会持有额外CUDA context，计入显存预算。
- 当前组件支持连续H264或单层HEVC+可选AAC及已限定视频输入；未验证任意MP4/多层HEVC/任意64位时间戳，不宣称通用容器库。

## 发布建议

暂不发布；继续处理上述待验收范围，完成最终SHA验收后再形成具体上线结果。GitHub-first、私有备份、新venv、只读/本地doctor/COS分层步骤见deploy.md。

如需暂停制作，先告知用户，由用户暂停并确认；不得主动drain/restart/switch。当前node与publish共用unit，准确说明局部重建影响，不暂停独立发布服务或全站发布。QA本轮未执行生产stage、暂停、重启、切换或社媒发布。
