# CPU 查询 / 香港 GPU 制作边界（2026-08-27）

用户确认：所有业务查询放 CPU，香港 GPU 只制作、上传 COS 并回传结果。此次不改已验收的页面风格、短链格式、YouTube 频道/评论合同或现有其他平台业务。

截至北京时间 14:47，代码与 CPU 实机文件验证通过；CPU 正式 API/worker 未替换或重启，尚未切流，不能称已完整上线。CPU 新候选是 `40042f9692fbec58caa5abbf41af35e9aefb54bc`，HK 实际运行仍为 `e1f5a1d04cfb510df9c2444ac592adec2827508b`。正式部署仍受既有数据库合法写权限门禁约束。

## 单向职责

| 环节 | CPU 43.166.187.96 | HK GPU 43.154.250.89 |
| --- | --- | --- |
| 查询与选择 | 剧集/分集/素材 URL、模板目录、自动/手动配方、任务状态、频道与账号 | 不查询业务数据库或 CPU 业务 API |
| 制作请求 | 下发 job 信息、已解析媒体 URL、输出选项、封面参数、冻结配方 | 完整参数校验、下载、合成/去 BGM/封面/随机模板 |
| 成片 | 校验回传配方并保存结果与状态 | 上传 COS，回传产物 URL 及制作元数据 |
| 后续业务 | 复制链接、生成 gy 短链、OAuth、YouTube 上传/评论、三表同步 | 不参与 |

缺失必填素材或配方时拒绝制作，不让 GPU 自行查库补参数。GPU 的本地模型/素材/manifest 校验、本地完成标记、COS 上传和成片 HEAD 可用性校验属于制作环节，不是业务查询。GPU 不启动主应用的任务恢复和数据库初始化。

## 发现与修正

旧 CPU `drama_random_template_catalog()` 在没有本地素材目录时会请求 GPU `/api/gpu-video/random-overlay/catalog`，不符合这次明确的职责边界。已移除该 HTTP 路径。

- `features/drama_synthesis/catalog.py` 从 CPU 原始 manifest 构建目录；只用标准库及既有合同常量，无网络/数据库调用，不需要 520 MB 素材包。
- 路径必须绝对、regular、非 symlink，文件大小 1 byte 至 2 MiB（含边界）；校验打开后文件类型、实际长度及 SHA，拒绝重复 JSON 键、错误版本/类别/资产字段、非法名字/大小/类型。
- 原始 light 元数据也校验，但目录仅返回 border/opacity_video/corners/tint，数量 3/5/3/7，组合 315；资产 identity、顺序、recipe/profile 与现行 GPU 一致。
- CPU 配置缺失/错误返回脱敏 503，不回退 GPU 或媒体素材目录；HK 本地诊断仍可读取本机素材，供制作预检使用。

## 已落地的 CPU 目录与 GitHub-first 验证

原始文件来自已验收的 HK FB-v3 manifest，经已知主机密钥 SSH/SFTP 传输。CPU 只新增以下文件及其两层专用目录，没有改生产环境变量：

```text
/mnt/data-disk/drama-synthesis-catalog/fb-v3-028326ab2114/manifest.json
SHA256 028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f
7921 bytes; root:root 0444; parent directories root:root 0755
```

安装前确认目标不存在、数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`；新临时文件排他创建、回读一致后不可覆盖 rename，再校验最终 SHA/owner/mode。没有覆盖旧文件、复制素材包或修改数据库。

代码先提交并推送 GitHub，再由 CPU 独立拉取精确 SHA，验证目录为：

```text
/mnt/data-disk/drama-synthesis-cpu-validation/releases/40042f9692fbec58caa5abbf41af35e9aefb54bc
```

这是 detached、干净的 sparse checkout，仅用于验证，不是生产 API 或 systemd 服务目录。CPU Python `3.9.6` 读取真实 7921-byte 文件，并执行从该 SHA `app.py` AST 提取的原函数：四层/315、自动和手动冻结/重复一致均 PASS；socket/SQLite/HTTP/素材包 tripwire 调用均为 0；缺配置及错 SHA 均返回 503，无 fallback。未 import 整个生产应用，也未伪称线上 HTTP 已切换。

两端 Git blob 读回一致：`app.py` 为 `72048b224b473538c8c99495af02c1e7f9d9abcb`，`catalog.py` 为 `0d2b9d5e8ae32a26cd928386dc808846d6154fc0`。CPU 实机自动/手动 recipe SHA 分别为 `766663074f49f7a19045134154a6191d0a845fc841b554f357e95a59b55d78ca`、`ffff27fbdedb4079050e4e6af2f1d09ad704e88973862783ea7546b275d96fbf`；使用离线验证 identity，没有创建生产任务。

## 独立 QA

唯一一次七套合并回归 **204/204 PASS，13.639 秒**：33+46+7+56+24+22+16。实现者先跑的 16 项已包含于 204，不相加。另 15 项纯内存对抗 PASS，单列，不算第二次整套。

```sh
python -B -m unittest scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_upgrade scripts.test_drama_youtube_unified_rpc scripts.test_drama_youtube_canary scripts.test_drama_youtube_three_table_rehearsal scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_cpu_catalog -v
```

独立复核了 descriptor 类型变化、文件增长/截断/同长改写、重复 JSON 键、深嵌套/非 UTF-8/NaN、错误脱敏和绝不回退 GPU。3 个 Python 文件内存 compile/Python 3.9 AST、diff-check PASS；4 个冻结文件测试前后 SHA 一致，无新增 P0/P1。离线回归不替代真实媒体或 YouTube 发布验收。

最终 13 份文档经独立一致性复核 PASS；受影响的部署/迁移文档合同用例定向复测 1/1 PASS（0.034 秒），不叠加 204，也未再次运行整套。大小范围措辞已改为明确的 byte 下界，代码无变动。

制作调用链另有只读静态复核：56 个本地模块、专用 worker 未调用 `app.main()`；`_gpu_worker=True` 不写 CPU 任务 SQLite，缺参不反查。共享应用导入仍有日志/锁等初始化，不能表述为“绝对零副作用”。本次未改 HK renderer/cache，沿用 e1f5 已完成的真实双模式与重启复用证据，不宣称本轮重新制作。

既有非阻断 P2：共享媒体下载器允许输入 URL/重定向，恶意服务令牌持有者或错误媒体跳转可能发出非预期 HTTP 请求；本次证明的是正常业务调用边界，不是网络级强隔离。后续加固应使用剧集合成专用来源/路径/重定向白名单，不擅改会影响 X/FB/TT 的全局下载器；没有观测到实际 GPU 业务查询事故。

## 线上未改动与下一门禁

- 14:47 CPU API PID `3841722`、job worker `1212`，active、NRestarts=0；生产 `app.py` SHA 仍为 `a956fb9952aa09d8d911cf3a5c54b58525cb81935d92d0ede698af9c681675a3`。14:27 读回仍指向 legacy `18787`；此次未更改其配置。
- 14:27 HK worker/tunnel active、health `media-only`，实际进程无非空 MYSQL/ADMIN_MAPPING_MYSQL/FEISHU/YOUTUBE_OAUTH/X_OAUTH 业务凭据；任务 SQLite 文件不存在。此次无 HK 服务/环境/模型/素材变更。
- 原 c719beb 三表恢复证据仍只绑定原候选，不能改绑 40042f9；正式迁移前按新候选重验/更新新鲜证据，不覆盖旧目录。
- 当前 `ads_aius` 对目标库仍只有只读授权；合法 migrator/writer 尚未具备。未运行生产 DDL、打开 live/sync、生成真实 YouTube 短链、刷新 OAuth 或上传/评论。
- 正式发布时在 CPU API 和实际任务 worker 的配置源同时设置本地 manifest 绝对路径及固定 SHA。HK 留空 CPU-only 文件变量，保持其本地素材根。备份配置、门禁/drain 完成后再发布 CPU 40042f9、切 18788；HK 正式 COS 前缀激活与指定 Shahrul Ikmal 单次 unlisted/评论验收仍按既定方案执行。

回滚：目前只新增只读目录及隔离 checkout，无需改动运行中的服务；保留它们作为证据即可。将来 CPU 激活后需一起回退应用和对应配置，不在 manifest 失效时重新启用 GPU 查询 fallback，不删除已完成 COS/短链/发布记录。
