# 测试用例

## 测试状态

代码与自动化已实现，阶段性本地运行已完成；本表保留逐项验收状态，只有最终 diff 的全量自动化、隔离浏览器或生产只读证据完成后才能把对应行改为“通过”。除明确标注“生产只读”的项目外，全部使用临时 SQLite、fake resolver、fake Redis 和隔离浏览器；不得连接 TikTok publish/canary 接口。

## 测试数据

| 数据 | 用途 |
| --- | --- |
| code `AB12` / 输入 `ab12` | 大写归一和精确查询 |
| content ID `LZ4b4w5k3h` | 有多条 published route 的同剧搜索 |
| content ID `Ag0rfr5F0F` | 无 route 的 generic fallback |
| queue 101 / 102 / 103 | `published_at,created_at,queue_id` 最新排序与并列时间兜底 |
| 含中文、空格、`&`、`#`、方括号的素材/剧名 | URL 编码 |
| fake Redis 6381 | hit/miss/timeout/陈旧缓存/恢复 |
| 可注入两字符两位小空间 | 穷尽、最早回收和并发模型，不占用生产空间 |

## A. 新旧页面与路由

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| A01 | 打开新页面 | GET `/tt-code` | 200、无 Location、`no-store`，加载新 HTML/JS | P0 | 待执行 |
| A02 | 原页面不变 | 比较部署前后 `/tt`、原 HTML/JS hash 与浏览器主流程 | 文件 hash 与行为不变 | P0 | 待执行 |
| A03 | 路由隔离 | 请求 `/tt-code/`、未知脚本、POST `/tt-code` | 只允许约定 exact GET；非法形状拒绝 | P1 | 待执行 |
| A04 | 移动视口 | 390x844 打开新页 | 无页面级横向溢出，输入、结果和 Featured 可用 | P1 | 待执行 |
| A05 | 桌面视口 | 1440x900 打开新页 | 布局不拉伸失真，左右按钮可见/可用 | P1 | 待执行 |

## B. Featured 横向交互

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| B01 | 动态列表 | Featured API 返回合法新鲜五条 | DOM 恰好五张卡，无第六条 | P0 | 待执行 |
| B02 | fallback 列表 | Featured 超时/500/过期/非法 | DOM 仍恰好五张安全 fallback 卡 | P0 | 待执行 |
| B03 | 触摸横滑 | 手指横移超过阈值并抬起 | 列表滚动，未调用 resolver、未导航 | P0 | 待执行 |
| B04 | 触摸轻点 | 点击一张 Featured 卡 | 调用 resolver `source=Featured`，成功后导航 | P0 | 待执行 |
| B05 | 鼠标拖动 | 按下后横拖超过阈值 | 列表滚动，不触发卡片 click | P0 | 待执行 |
| B06 | 鼠标点击 | 无显著位移点击卡片 | 只触发一次解析与导航 | P0 | 待执行 |
| B07 | 左右按钮 | 连续点击左右按钮 | 按视口滚动，首尾按钮状态正确，不误点卡片 | P1 | 待执行 |
| B08 | 键盘操作 | Tab 聚焦按钮/卡片并 Enter/Space | 焦点可见，按钮滚动，卡片按预期解析 | P1 | 待执行 |
| B09 | 纵向页面滚动 | 从 Featured 区域纵向滑动页面 | 页面可纵向滚动，不被横向容器锁死 | P1 | 待执行 |
| B10 | reduced motion | 开启 `prefers-reduced-motion` | 不使用强制平滑动画，功能不受影响 | P2 | 待执行 |
| B11 | 快速重复手势 | 连续拖动/点击不同卡片 | 旧请求取消或失效，最终只导航最新有效点击 | P1 | 待执行 |

## C. code schema、分配和回收

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| C01 | 空库迁移 | 对临时旧版 DB 执行 ensure storage | 加法创建 route/audit 表、索引、trigger 和 queue.code，旧表/行不变 | P0 | 待执行 |
| C02 | 重复迁移 | 连续执行迁移两次 | 幂等成功，schema/数据一致 | P0 | 待执行 |
| C03 | code 格式约束 | 插入小写、3/5 位、符号、Unicode | DB CHECK/业务校验拒绝 | P0 | 待执行 |
| C04 | 正常分配 | 模板含 `{code}` 的新正式 queue freeze | 产生 `^[A-Z0-9]{4}$` code，queue.code、route.code 与 caption 中的值一致 | P0 | 待执行 |
| C05 | 大小写输入 | 查询 `ab12`，库内为 `AB12` | 统一命中 `AB12` | P0 | 待执行 |
| C06 | 人工碰撞 | 随机源先后返回已用 code、空闲 code | 不覆盖旧行，安全重试后插入空闲 code | P0 | 待执行 |
| C07 | 并发分配 | 多存储实例并发创建不同 queue | code 与 queue 均唯一，无锁错误泄漏 | P0 | 待执行 |
| C08 | 同 queue 并发幂等 | 并发重放相同 queue 身份 | 只保留一行、返回同一 code | P0 | 待执行 |
| C09 | 同幂等键事实冲突 | 同 queue/键但 content/归因不同 | 409，原 route 不变 | P0 | 待执行 |
| C10 | 高占用随机兜底 | 小模型空间仅剩一个空槽，随机持续碰撞 | 有界后确定性找到剩余槽，不误判用满 | P0 | 待执行 |
| C11 | 空间用满回收 | 小模型空间全占用，时间可排序 | 同一事务删除最早 `created_at,code` 并以该 code 写新 route | P0 | 待执行 |
| C12 | 未用满禁止回收 | 小模型空间仍有空槽但随机碰撞 | 绝不删除最早行，最终找到空槽或明确失败 | P0 | 待执行 |
| C13 | 回收并发 | 两个 writer 在满空间同时分配 | 串行事务，各自行为可解释，无重复 PK/丢失新行 | P0 | 待执行 |
| C14 | 回收审计 | 发生满容量回收 | `tt_post_code_recycle_audit` 记录旧 code/queue/content、新 queue 和时间，不含完整 URL | P1 | 待执行 |
| C15 | 事务回滚 | code 已分配但 caption 校验失败 | route、queue、事件整体回滚，不消费 code | P0 | 待执行 |
| C16 | SQLite 完整性 | 迁移/碰撞/回收后执行 PRAGMA | `integrity_check=ok`，FK/唯一索引有效 | P0 | 待执行 |

## D. `{code}` 宏与 caption

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| D01 | 精确宏 | 模板含 `{code}` | queue 最终 caption 替换为分配 code | P0 | 待执行 |
| D02 | 非法变体 | `{CODE}`、`{{code}}`、`{ code }` | 400 `caption_placeholder_invalid` 或稳定等价码 | P0 | 待执行 |
| D03 | 单次非递归 | `{desc}` 的值中含 `{code}` | description 字面 `{code}` 保留，模板 token 只替换一次 | P0 | 待执行 |
| D04 | preview 不消费 | 连续预览含 `{code}` 模板 | route 行数不变，页面明确显示示例/待分配 | P0 | 待执行 |
| D05 | 多次宏 | 模板重复三个 `{code}` | 三处均为同一 code | P1 | 待执行 |
| D06 | 与其他宏组合 | `{desc}`、`{url}`、`{code}`、`{{content_id}}` | 各宏按冻结值一次渲染，无残留 | P0 | 待执行 |
| D07 | 2200 UTF-16 边界 | 渲染后恰好 2200 units | 接受 | P0 | 待执行 |
| D08 | 2201 UTF-16 边界 | 渲染后 2201 units | 拒绝且 route/queue 回滚，不截断 | P0 | 待执行 |
| D09 | 正式模板不含宏 | 新正式 queue 的模板不含 `{code}` | caption 行为不变，但 queue.code 与 route 仍生成同一个有效 code | P0 | 待执行 |
| D10 | 发布重试 | 同 queue 第一次失败后重试 | caption/code 均不变化，不新增 route | P0 | 待执行 |
| D11 | 直接测试宏边界 | direct test 模板含 `{code}` | 409 `tt_post_code_macro_queue_only`，不创建 code/route、不调用 publish | P0 | 待执行 |
| D12 | 历史 queue 兼容 | 迁移前 pending `{url}` queue 无 code/route | 继续按原 AIpost 长链冻结并可进入原发布流程，不强制补 route | P0 | 待执行 |

## E. 归因字段与 URL 编码

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| E01 | `c` 格式 | 冻结用户名、秒级时间戳、语言、剧名、标签、queue 101 | 精确为 `yingliang_post_CLV_VL_...*101` | P0 | 待执行 |
| E02 | `c` 尾部身份 | code=AB12、queue=101、publish_id 另值 | `c` 尾部和 `af_c_id` 为 queue 101，不是 AB12/publish_id；有 `{url}` 时 short_link_id 也按既有合同复用 queue ID | P0 | 待执行 |
| E03 | 正式 channel | 生成正式发布 route | `af_channel=TT` | P0 | 待执行 |
| E04 | 字段映射 | 检查 page/material/content/queue | 所有 `af_*` 与需求逐项一致 | P0 | 待执行 |
| E05 | 参数顺序 | 解析原始 query | 顺序为 `af_dp,c,af_adset,af_adset_id,af_ad,af_ad_id,af_channel,af_c_id` | P1 | 待执行 |
| E06 | 特殊字符编码 | 名称含中文、空格、`&`、`#`、方括号 | 解码后值不变，原 URL 无参数注入或截断 | P0 | 待执行 |
| E07 | 星号合同 | `c` 含分隔 `*` | 分隔可读且 round-trip 值准确 | P1 | 待执行 |
| E08 | 目标 allowlist | 尝试 http、其他 host/path、端口、userinfo | fail closed，不持久化/不导航 | P0 | 待执行 |
| E09 | `af_dp` 一致性 | target 的 `af_dp` 与 route content 不同 | 拒绝 | P0 | 待执行 |
| E10 | 冻结重放 | 源账号/素材名称发布后变化 | 已存 route/target 不漂移 | P0 | 待执行 |
| E11 | 旧 channel 兼容 | 历史 pending queue 与 direct test 构造 URL | 保持 `af_channel=AIpost`；只有新正式 queue 为 TT | P0 | 待执行 |

## F. 公共 resolver 与路由模式

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| F01 | code exact | `query=ab12&source=Search`，AB12 已冻结 | `query_type=code`、`route_mode=code_exact`，target channel 仍为 TT | P0 | 待执行 |
| F02 | 非 published 状态 | 分别以 scheduled/failed/unknown 等已冻结 AB12 查询 | 均按主键命中同一冻结 route，响应不泄露内部 state | P0 | 待执行 |
| F03 | 未知 code | 无 AB12 | 404 `found=false`、`error=code=tt_code_not_found` | P0 | 待执行 |
| F04 | 直接 ID clone | 同剧有多条 published | 按 `published_at DESC,created_at DESC,queue_id DESC` 选最新，仅 channel 改 Search | P0 | 待执行 |
| F05 | published 时间并列 | 多条同 `published_at` | 先选 `created_at` 较新；仍并列时选 queue ID 较大 | P0 | 待执行 |
| F06 | 直接 ID fallback | 同剧无 published | `c=TTpost`、`af_c_id=0001`、channel Search | P0 | 待执行 |
| F07 | Featured clone | `source=Featured` 且有 published | clone 后仅 channel Featured | P0 | 待执行 |
| F08 | Featured fallback | `source=Featured` 且无 published | generic 参数加 channel Featured | P0 | 待执行 |
| F09 | clone 不写库 | 重复 Search/Featured 查询 | route/code/queue 行数与内容不变 | P0 | 待执行 |
| F10 | content ID 大小写 | 错误大小写 content ID | 按现有 resolver fail closed，不转大写猜测 | P0 | 待执行 |
| F11 | 非法 query | 3/5 位短值、特殊符号、超长、重复 query | 400，不查询源库/Redis | P0 | 待执行 |
| F12 | 非法 source | `search`、`TT`、空、重复 source | 400；只接受精确 `Search|Featured` | P0 | 待执行 |
| F13 | 剧不存在 | code route 或 fallback 指向的 content 无法由现有剧 resolver 确认 | 公共接口统一 404 `not_found`，无 CTA | P0 | 待执行 |
| F14 | 上游异常 | 剧 resolver / sidecar / SQLite 异常 | 返回稳定 5xx（剧 resolver/连接超时为 503，sidecar 未预期存储异常为 500），不伪装 404、不泄露细节 | P0 | 待执行 |
| F15 | 响应安全 | 检查 JSON/headers | 无内部 token、DB/Redis 地址、SQL 和堆栈；`no-store` | P0 | 待执行 |
| F16 | 限流/并发门禁 | 超过 token bucket / in-flight | 429/503 稳定返回，不拖垮服务 | P1 | 待执行 |

## G. Redis 读缓存与降级

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| G01 | cache hit | Redis 有合法且当前的完整 route DTO | 返回正确路由；公共响应不暴露 Redis key/地址，也不依赖 code-cache header | P1 | 待执行 |
| G02 | cache miss | Redis 无 key，SQLite 有 route | SQLite 返回并安全回填；公共语义与 hit 一致 | P0 | 待执行 |
| G03 | Redis 停止 | 6381 拒绝连接 | SQLite 仍返回正确 found/404；Redis 故障本身不产生 5xx | P0 | 待执行 |
| G04 | Redis 超时 | fake Redis 不响应 | 在短超时内回 SQLite，不拖到页面超时 | P0 | 待执行 |
| G05 | 非法缓存 JSON | key 内容损坏/字段缺失 | 丢弃并读 SQLite，不向公网报解析细节 | P0 | 待执行 |
| G06 | 负缓存 | 未知 code 第一次 miss 后重复查询 | 短 TTL 负缓存生效；新发布写入后及时失效 | P1 | 待执行 |
| G07 | 回收陈旧值 | Redis 有旧 AB12，SQLite 已回收为新 route | 写事务内让旧 namespace 不可达；旧 target 不再返回，DELETE 失败不影响 SQLite | P0 | 待执行 |
| G08 | 写缓存失败 | SQLite commit 成功、Redis SET/DEL 失败 | 发布事实保留；namespace 旋转/SQLite fallback，无 500/数据回滚 | P0 | 待执行 |
| G09 | Redis 恢复 | 故障后服务恢复 | 安全刷新 key 后恢复 hit，不复活旧值 | P0 | 待执行 |
| G10 | 监听边界 | 生产检查 6381 | 仅 `127.0.0.1`/`::1`，公网连接失败 | P0 | 待执行 |
| G11 | 慢 Redis 锁边界 | fake GET/DELETE 暂停，另一线程获取共享 queue write lock | 网络 I/O 期间共享锁可取得；恢复后查询/失效完成且不复活旧 namespace | P0 | 待执行 |

## H. 部署、回滚与零真实发布

| 编号 | 场景 | 前置条件 / 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- |
| H01 | GitHub exact commit | 候选通过后 push，服务器 checkout exact SHA | release 内容与 GitHub commit 一致 | P0 | 待执行 |
| H02 | 数据盘门禁 | 检查 mount UUID/空间/权限 | 正确挂载且可写，否则停止部署 | P0 | 待执行 |
| H03 | DB 副本迁移 | 对 online backup 副本启动新代码 | schema/数据计数符合预期，integrity ok | P0 | 待执行 |
| H04 | 备份完整性 | DB/静态/Nginx/env/systemd/release 备份 | manifest/hash 可校验，敏感文件仍 root-only | P0 | 待执行 |
| H05 | Nginx 验证 | `nginx -t` 与 exact route smoke | 配置通过，新页/API 可达，原 `/tt` 不变 | P0 | 待执行 |
| H06 | 服务最小重启 | 仅重启受影响 unit/reload Nginx | health 正常，GPU/无关服务不动 | P1 | 待执行 |
| H07 | 零真实发布 | 比对队列/publish ledger/runner 请求并审计调用 | 验收未调用 publish/canary/run-now/schedule-save | P0 | 待执行 |
| H08 | 代码回滚 | 切回上一 release、恢复静态/Nginx/env/unit | 原服务恢复，新页关闭/旧页正常，SQLite 安全保留 | P0 | 待执行 |
| H09 | Redis 回滚 | 停止独立 6381 实例并恢复 unit/config | 旧代码不依赖 Redis，无事实数据丢失 | P1 | 待执行 |

## 回归范围

- 既有 `{url}`、`{desc}`、`{{content_id}}`、`{{contect_id}}`。
- TT 素材 intake、准备、recurring pool、自动和 direct-test queue、账号设置与随机排期。
- `/tt` resolver、Featured JSON、旧 W2A/短链构造。
- X 短链、GPU prepare/profile 与发布门禁不受影响。
- SQLite 老库迁移、重启、并发和回滚。
