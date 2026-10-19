# X 发布随机模板开关

2026-10-19。仅 X；自动发布模板、素材池定时、短剧池定时、素材池手动发布四处新增 `random_template_enabled`，全部缺省 false，严格接受 JSON boolean。图片跳过视频制作。四处各自保存，手动每次新批次恢复关闭。

自动模板配置版本、池排程版本/随机日计划/运行批次/队列逐级冻结；开启只影响对应的新任务。旧数据增量迁移默认 0，数据库触发器禁止修改冻结值或队列与所属批次不一致。排程生效时间继续沿用已有固定/次日随机规则。手动提交指纹与服务端幂等都包含该字段。

发布前继续原媒体解析、必要修复与会员路由。在凭证锁外调用 `POST /internal/x-post-random-template`（复用现有 18820 SSH 隧道和内部 token）。GPU 与媒体修复共用同一并发锁；使用独立 `state/random-template` 与 COS `random-template` 命名空间。首次请求在 GPU 原子写入请求、目录版本及配方，重复请求校验已有 COS 指纹后返回同一成片。CPU 在 `x_post_random_template_preparation` 保存不可变请求及结果（含源/成片 SHA256、大小、时长、配方）。不覆盖源素材或历史成片。

GPU 复用线上 OpenCL/NVENC 随机效果、透明原片叠层、已验证 RGBA 素材缓存和停滞恢复组件。制作不缩短视频，成片与输入时长差 <= 0.05 秒且不得跨 140 秒边界；最终 H264/AAC 720×1280、512 MiB 门禁不变。普通账号 140 秒、会员 4 小时、自动模板 600 秒限制均保留。制作超时、源指纹改变、回执不符或最终媒体不合格均停止任务，禁止改发原片。Premium 中转只给源 Post 制作视频，目标账号仍转发既有 Post。

## 验证

- `python -W ignore::ResourceWarning -m unittest discover -s scripts -p 'test_x*.py'`：976 项通过，2 项环境条件跳过。随后 GPU 最终校验改用原媒体修复的严格编码校验器，34 项相关回归通过。
- 新测试覆盖独立默认值、保存/冻结、手动和自动幂等、重复制作、源/成片指纹、超时、历史尝试阻断、图片跳过、长视频门禁，以及模拟上传和 Post 防重复。
- 本地 Playwright 隔离接口验收四处控件和只对视频说明，验证模板回显、手动批次每次关闭、池设置互不影响。无需生产数据写入。
- `scripts/verify_x_random_template_live.py` 使用真实 GPU/媒体处理和隔离 SQLite；X HTTP 客户端始终是模拟客户端，无真实 X Post。生产验收结果追加到 deployment.md。

## 部署与回滚

先推送 GitHub，服务器 fetch 精确提交。`scripts/deploy_x_random_template.py --host gpu|cpu` 从实际运行的组合目录复制独立 release，只覆盖白名单文件；CPU 与主应用源文件先校验实际代码基线。保留已有其他功能。部署前备份公开静态文件、主应用代码和数据库在线快照；在副本重复执行迁移并校验完整性、行数和全关闭默认值。CPU 持有原发布锁、临时停止原活动定时器，再窄范围切换服务，最后恢复原定时器状态。GPU 先部署验收，再 CPU。

部署输出备份目录及精确 release 路径。回滚：在对应 GitHub checkout 使用 `python3.9 scripts/deploy_x_random_template.py --rollback <deployment-output-backup-path>`。数据库、token、发布账本及制作回执始终保留。CPU 回滚检查已开启配置、待执行队列/批次/随机计划及自动模板版本；发现开启历史时先停止 X 定时器和发布 sidecar 并阻止代码回退，由操作者对精确任务进行核查、暂停后再继续，不能让旧版本绕过制作。GPU 回退使新 CPU 的随机制作失败并停止该任务，不会发送原片。
