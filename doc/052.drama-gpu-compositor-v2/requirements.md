# 052.drama-gpu-compositor-v2 需求与技术设计

## 背景

现行随机模板由一条超长 FFmpeg CPU filter graph 完成。720x1280 长视频会在两核配额下持续低于实时速度，单次失败会丢失整段随机模板渲染。香港节点为 Tesla T4 16GiB，具备 OpenCL、CUDA、NVDEC 和 NVENC 能力，但现行实现只使用 NVENC。

## 目标

- 建立版本化 Composition Spec，模板与渲染执行解耦。
- 将缩放、裁剪、任意角度旋转、染色、圆角/边框与多层 Alpha 合成融合为一个 GPU shader pass。
- 将长视频切分为可独立校验、缓存和重试的固定帧分片。
- 保持现有 CPU/GPU 异步任务、COS、随机配方及结果字段兼容；不继承旧
  `rotw(iw)/roth(ih)` 造成的横向断层、黑块和异常裁切。
- 生产保持单个完整任务；每个任务内部支持 1～4 条独立分片 lane，T4 实测默认采用 2 条。

## 范围

### 包含

- DramaWave 随机模板视频输出。
- OpenCL fused compositor、NVENC、分片合并及独立音频 mux。
- 内容寻址的场景、kernel、分片和最终成片检查点。
- GPU worker 配置、健康信息、进度与回滚文档。

### 不包含

- 修改随机素材业务选取概率或历史随机配方。
- 修改去 BGM 模型、剧集下载、封面生成和 COS 凭据。
- 自动重试已失败的正式任务；旧任务恢复需在新版本稳定后单独执行。
- 在本需求中迁移 FB/TT/X 现有渲染调用方；Composition Spec 为后续迁移预留能力。

## 用户故事 / 业务规则

1. 制作人员提交长剧集后，随机模板失败最多重做一个分片，而不是从第 0 秒开始。
2. 同一源文件、配方、素材指纹及渲染器版本必须得到同一 Composition ID，并复用已验证分片。
3. 不支持的 backend、scene 字段或素材合同必须显式失败，禁止静默回退 CPU。
4. 最终公开结果仍使用 `output_random_template_url`，配方与产物 SHA 合同不变。

## 交互与流程

CPU 异步提交 → GPU 下载/拼接/去 BGM → 编译 Composition Spec → 按帧规划分片 → GPU fused render → 分片校验 → stream-copy 合并 → 独立音频 mux → COS 上传 → CPU 轮询完成。

## 技术设计

### 影响模块

- `features/drama_synthesis/composition.py`：场景协议、规范化与分片规划。
- `features/drama_synthesis/gpu_compositor.py`：shader、命令编译、检查点、合并和 mux。
- `features/drama_synthesis/gpu.py`：backend 路由与全局进度映射。
- `scripts/drama_synthesis_gpu_worker.py`：1～4 lane 配置和健康能力快照。
- `deploy/drama-synthesis-gpu/worker.env.example`：V2 配置合同。

### 数据结构

Composition Spec v1 包含 canvas、timeline、audio、layers、output 和 renderer profile。Composition ID 为规范 JSON 的 SHA-256。分片 identity 额外包含源文件指纹、分片规划、起始帧、帧数、kernel SHA-256、候选 release、FFmpeg 二进制和 GPU/驱动指纹。shader 每帧读取输入图像尺寸，支持同一流横竖分辨率切换。

### API / 接口

外部 API 不新增必填字段。GPU `/healthz` 增加非敏感 renderer、backend、chunk 和 lane 能力；任务状态继续通过现有 `/api/gpu-video/jobs/{job_id}` 返回。

### 异常与边界

- OpenCL 初始化、kernel 编译或硬件能力不满足：`drama_gpu_compositor_unavailable`。
- 分片渲染失败/超时：保留已完成分片，返回安全错误，不删除已验证结果。
- 分片身份或最终检查点冲突：沿用 fail-closed checkpoint 错误。
- GPU backend 为 V2 时禁止调用 legacy CPU graph。

## 验收标准

- 当前随机模板全部视觉元素存在，输出为单个连续 9:16 画面；不得出现对比拼图、
  黑块、横向断层、错位裁切或分片边界跳帧。
- 主体层按正确旋转角围绕画布中心缩放/旋转；renderer profile 必须升级，禁止复用
  曾按 legacy 错误几何生成的分片或最终检查点。
- 输出 H.264 High、720x1280、30fps，音频和时长合同不变。
- 任意分片失败后再次执行，只启动失败及后续未完成分片。
- 以截图失败任务的约 79.4 分钟同源素材做完整长样；不以预设最低倍速替代效率选型，采用同机实测最快且稳定的配置，记录吞吐并以 3x realtime 为优化目标；无新增 swap，RSS/VRAM 有界。
- 通过 Composition/renderer 单元测试、现有 drama 回归、真实 T4 小样和长样基准。
- GitHub exact commit 部署；CPU/GPU 均有独立备份、窄服务重启和明确回滚。

## 风险与待确认

- FFmpeg OpenCL 与 NVENC 之间当前需要一次受控硬件帧下载/上传；若实测成为瓶颈，再升级 CUDA 原生互操作后端，Composition Spec 与分片合同不变。
- Alpha、色彩空间和双线性采样与旧 CPU graph 可能存在像素级差异，验收采用结构/抽帧相似性和人工视觉对照，不要求逐字节一致。

## 变更记录

- 2026-09-01：按制作效率和泛用性优先原则创建 V2 需求与技术设计。
- 2026-09-01：用户拒绝 legacy 错误几何兼容成片；验收基线改为正确角度、连续画面的 clean profile。
