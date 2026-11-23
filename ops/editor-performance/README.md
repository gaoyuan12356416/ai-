# 智能剪辑：完整选择与准备性能

范围仅限独立剪辑 API、静态工作台、低优先级预览进程和 Nginx 内部派生资源。
模型、原片、GPU 渲染算法、主后台及社媒发布服务不变。

## 行为与兼容

- 服务端 `studio_source_batches/items` 保存有序选择；`source_ids` 仅投影已就绪素材。
- 操作编号在 `operations` 保存原回执，响应丢失后先核对回执；版本冲突保留浏览器草稿。
- 单素材解析按账号、内容 SHA、时长、音轨、算法和模型配置共享，语义 24h／降级 120s。
- 新增子任务及派生表均增量创建。数据库事务不包含模型、下载或 FFmpeg。
- 等待素材的制作意图在明确移除后被新选择替代；已开始制作的输入保持冻结。
- 单条失败不阻断其他条目；预览失败不影响解析及制作。
- 导入 1–3 路可配置；抽帧最多 2 路，视觉调用 1 路；预览独立进程 1 路、FFmpeg 2 线程。
- 预览目录单独授予 Nginx 读取权限，原片目录权限保持不变；所有派生入口先验证素材归属。
- 首屏 24 项图片卡片；交互局部更新；进度 2s，隐藏 10s，失败 4/8/15s 退避。

## 验证证据

本次同环境、同 5 集、独立冷数据库比较：

| 导入并发 | 总秒数 | 期间列表 HTTP P95 | HTTP 采样 | 错误 |
|---|---:|---:|---:|---:|
| 1 | 38.583 | 3.95ms | 153 | 0 |
| 2 | 20.914 | 3.52ms | 83 | 0 |
| 3 | 15.079 | 3.60ms | 60 | 0 |

全部下载内容均与原 5 集 SHA-256 一致。选择 3 路，较同环境 1 路约快 61%；不以历史录屏 288 秒作为同环境基线。
本次为单轮受控比较，公网与供应方随时间波动，不能保证每次冷导入均为 15 秒。

浏览器可见页面 20 次勾选，点击到下一帧 P95 12.5ms，Event Timing 交互最大 32ms。
后台标签页计时受浏览器节流影响，未混入可见页面结果；这不是长期真实用户 INP 分布。
实际验证了：素材弹窗无视频元素、全选保存、乱序完成顺序不变、关窗刷新保留、播放器节点及 URL 不变、代理完成不抢播、服务端确认后丢失响应再刷新找回原选择。

相关 Python 回归新增选择、私有 HTTP 和真实预览验证；Node 覆盖混合选择、防重、上限、排序以及既有上传断点恢复。
原有 `test_real_http_render_silent_vfr_fractional_cuts_and_lost_completion` 在未修改基线上同样失败（成片逐帧／音轨校验），须独立列为既有失败，不能计为本次通过。
服务器组合特效测试须设置 `AI_VIDEO_EDITING_MIRROR_PYTHON=/usr/bin/python3`，它是测试环境参数，不修改生产 GPU 配置。

## 发布与回滚

1. 在 GitHub 推送精确版本，服务器独立 checkout，禁止覆盖现有脏目录。
2. `deploy.py install --repo CHECKOUT --version SHA --base 133f15b11635310e4779303ba615004c6ac10147 --compat-version COMPAT_SHA --workers 3`。
3. 发布脚本在线备份 SQLite，保存受影响代码、静态文件、配置、哈希和 manifest。
4. 仅短暂冻结独立 API 做原子空闲检查；若有下载、模型、FFmpeg、转码或 HTTP 请求则立即恢复运行并退出。可稍后用 `install --manifest PATH` 继续已准备的发布。不杀在途任务。
5. 切换后仅重启独立 API，新启预览消费者；Nginx 通过 `nginx -t` 后 reload。无需等待其他渠道/GPU 空闲。
6. 运行 `restore_selection.py`：仅当原会话版本、来源及更新时间均未变时，复用已入库 5 集，保留配置和历史，绝不创建制作任务。

默认回滚：

```sh
python3 CHECKOUT/ops/editor-performance/deploy.py rollback --manifest MANIFEST
```

它回退前端并切至保留新选择与消费能力的兼容代码，保留新预览服务和分发配置，防止丢失已经确认的待导入分集。
完整回滚（仅从仍为 active 的版本执行）：

```sh
python3 CHECKOUT/ops/editor-performance/deploy.py rollback --manifest MANIFEST --full
```

完整回滚额外要求本次不兼容选择、子解析、预览和等待素材任务均已安全结束；发现待处理/缺集任务即保持服务运行。
不恢复旧数据库，不删除原片、派生文件、解析缓存、创作输入或回执。配置并发可独立调小，但应用需等独立 API 安全切换点。

本次最终提交号、manifest 路径、线上浏览器截图、恢复回执及测试实际结果随发布验收记录提供。

## 浏览器验收后的静态补充

创作恢复直接复用首次会话响应，先显示完整素材和播放器，再读取解析详情。
隔离浏览器暂停解析详情请求后，5 张素材卡及可播放视频仍显示，初始化会话读取仅 1 次。
该补充只发布 HTML/JS，不切换 API、不重启服务，不修改任何任务或数据库。

```sh
python3 CHECKOUT/ops/editor-performance/static_update.py install --repo CHECKOUT --base PREVIOUS_STATIC_SHA --version SHA
python3 CHECKOUT/ops/editor-performance/static_update.py rollback --manifest STATIC_MANIFEST
```

需要回退整个版本时，先回退这个静态补充，再运行前述兼容回滚。
真实公网首次预览仍受链路波动影响，不能把缓存复播或服务器处理时间当作公网首帧达标证据。

## 剧目与分集读取补充

- 浏览目录元数据保存到独立 `catalog-browse.sqlite3`：5 分钟新鲜期，24 小时以内先返回上次结果并后台更新；同键冷读取合并，背景更新 1 路且有界。
- 只持久保存剧目/分集公开字段，下载 URL 始终排除；实际导入继续使用原 30 秒私有地址缓存和源校验。数据库读取只读验证、SQL Gate 和供应方间隔不变。
- 缓存按数据库来源及 Mobo 机构配置隔离；前端仅在已有模块权限时按账号使用 sessionStorage，刷新页面可恢复。提供更新时间、后台更新状态及手动更新入口。
- 打开媒资库预读两端首页，停留/聚焦剧目预读分集；移除只读操作的共享忙碌锁，慢读取时也能返回或切换，旧响应不得覆盖当前视图。
- 新增 7 个缓存测试，加原剧库 8 个和 HTTP 3 个均通过；浏览器暂停目录接口后刷新，剧目/分集可直接使用，待更新时仍可返回及切源。
- `catalog_probe.py --cache PATH --output PATH` 仅查询真实剧库并写入目录缓存和测量记录，不创建导入、解析、制作任务。
- 发布用 `catalog_deploy.py install --repo CHECKOUT --base API_SHA --static-base STATIC_SHA --version SHA`。在线备份与原有在途检查后，仅切换独立 API 和相关 HTML/JS；预览进程、主后台及其他执行服务保持运行。
- 回滚用 `catalog_deploy.py rollback --manifest MANIFEST`，恢复前一兼容 API/静态版本，保留全部素材、账本、5 集选择及新增的可丢弃目录缓存。

## 制作进度与成片入口补充

- 独立制作状态区放在播放器上方，依次显示素材准备、生成方案、排队制作、制作成片和成片完成。以子任务回执区分 `queued` 与 `running`，父批次 `rendering` 不再一律显示为正在合成；排队显示实际已等待时间，不编造编码百分比。
- 活跃制作持续读取会话与子任务回执，不因素材进度 ETag 未变而停止核对。完成后保留绿色状态、会话完成卡和“查看成片／下载成片”；通知不切走正在观看的原片。
- “本次成片”查看当前会话结果，“我的作品”保存全部历史结果、自动更新制作中任务，并提供预览和 MP4 下载。列表不为每张卡创建视频元素。发布文案按需展开，避免遮住播放器和下载按钮。
- 前端阶段推导新增 4 项测试；浏览器通过真实隔离 API 验证排队、领取、完成自动更新，父批次不变时仍更新，作品弹窗保留搜索、出现下载链接，完成不重置原片 URL，刷新后仍可查看成片。合成测试下载返回 206 和附件响应头。
- 仅发布 `video-editing-ai-production.js`、`video-editing-ai.css`、`video-editing-ai-studio.js`、`video-editing-ai.html`。`static_update.py` 支持显式 `--files`，保留旧文件并记录新增文件；回滚仅移除由本次创建且哈希仍匹配的新静态文件。安装/回滚与并行变更保护测试通过。

```sh
python3 CHECKOUT/ops/editor-performance/static_update.py install --repo CHECKOUT --base 4578f3b6993919c5b81d4947116d575917c9e7cc --version SHA --files video-editing-ai-production.js video-editing-ai.css video-editing-ai-studio.js video-editing-ai.html
python3 CHECKOUT/ops/editor-performance/static_update.py rollback --manifest MANIFEST
```

本次页面变更不调整 GPU 任务或容量保护，不重启 API、预览、GPU 或其他服务。实际任务仍排队时须如实说明未产生成片，不以隔离浏览器的模拟完成替代生产回执。
