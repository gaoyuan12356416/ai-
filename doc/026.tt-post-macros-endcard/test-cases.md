# 测试用例

## 测试范围

- M：caption 宏语法、单次渲染和 UTF-16 长度。
- F：`ads_drama_resource.desc` 的 intake/pool/queue 冻结、迁移和幂等。
- U：TT 专用短链、W2A 参数、wrapper 和 Nginx 兼容性。
- D：GPU `direct_outro`、旧媒体模式回归和 prepare-only。
- I：参考 X 素材池的 TT UI、自动排期控制和状态门禁回归。
- S：无真实发布证明、双轨回滚和外部副作用检查。

所有状态默认“待执行”。本文件是验收设计，不代表用例已经通过。

## 测试数据

| 数据 | 说明 |
| --- | --- |
| A | 可用 TT 账号；仅用于 preview/prepare；不调用发布接口，不改其 schedule |
| MAT-A | `ads_drama_resource.desc = "A frozen drama description"`，映射唯一、源 URL 不可变 |
| MAT-B | desc 中包含字面量 `{url}`、`{desc}`、`{{content_id}}` 和 emoji |
| MAT-C | desc 为空或只有空白，用于 fail-closed |
| CAP-2200 | 完整渲染后精确 2200 UTF-16 code units 的模板/数据组合 |
| CAP-2201 | 完整渲染后精确 2201 UTF-16 code units 的模板/数据组合 |
| OUTRO-V1 | 已审核固定片尾与圆角 Logo，记录绝对路径、SHA-256、size、duration/尺寸和抽帧 |
| JOB-NEW | 从未使用过的 prepare job ID；不得与生产 queue/job 重合 |
| DB-OLD | 不包含新列的 SQLite 副本，含已发布 queue 和 available pool 两类历史记录 |
| URL-TT/URL-X | 19 位 `8` 开头 TT URL 与一条现存 X 数字短链形状 URL |

## 执行前置与禁令

1. 保存 queue 记录数、GPU publish ledger 记录数、所有目标账号 schedule 的 `version/enabled/publish_times` 基线。
2. 关闭或隔离所有可能消费验收素材的 runner；不得通过修改生产 schedule 达到隔离目的。
3. 禁止调用 `/internal/tt-post/publish`、`/internal/tt-post/canary-publish`、`/api/admin/tt-posts/run-now` 和 `POST /api/admin/tt-posts/schedule`。
4. prepare-only 输出必须写 TT 专用 COS，并使用独立 object key。

## M. 宏与 UTF-16

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| M01 | 精确 `{url}` | 有合法 content/desc/short URL | 渲染 `Drama ID: {{content_id}}\n{url}` | 只把 `{url}` 替换为冻结短链，最终无未解析宏 | P0 | 待执行 |
| M02 | 精确 `{desc}` | MAT-A | 渲染 `Drama ID: {{content_id}}\n{desc}` | 只替换为 frozen description，来源可追溯 | P0 | 待执行 |
| M03 | 三类宏组合 | MAT-A 和合法短链 | 同一模板含 content、desc、url | 三类 token 各按模板位置替换一次，顺序不影响事实值 | P0 | 待执行 |
| M04 | 非法大小写/括号/空格 | 构造 `{URL}`、`{DESC}`、`{{url}}`、`{{desc}}`、`{ url }` | 分别 preview/入池 | 每种均以稳定 400 拒绝；不得当普通文本放行 | P0 | 待执行 |
| M05 | 非递归替换 | MAT-B | `{desc}` 后另有 `{url}`，desc 自身也含宏样式文本 | 模板 token 被替换，desc 内字符保持字面值，绝不二次解释 | P0 | 待执行 |
| M06 | 空 description | MAT-C，模板含 `{desc}` | preview/入池 | fail closed；不创建 intake/pool/queue/wrapper | P0 | 待执行 |
| M07 | 2200 正边界 | CAP-2200，含至少一个 emoji | 完整渲染 | 允许，记录 UTF-16=2200；不截断 | P0 | 待执行 |
| M08 | 2201 超限 | CAP-2201 | 完整渲染 | 400 拒绝；不消费素材、不创建 wrapper/queue | P0 | 待执行 |
| M09 | Drama ID 双别名 | 两个等价模板 | 分别用 `{{contect_id}}`、`{{content_id}}` 渲染 | 都得到相同真实 Drama ID，既有校验继续成立 | P1 | 待执行 |
| M10 | 无新宏历史模板 | 合法旧模板 | preview/入池/幂等重放 | 行为与旧版一致，无 description/URL 强制要求 | P1 | 待执行 |

## F. description 冻结与迁移

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| F01 | resolver 事实来源 | MAT-A | 查询解析结果和源行 | `description` 等于对应 `ads_drama_resource.desc` 清洗值，不取其他字段 | P0 | 待执行 |
| F02 | intake 首次冻结 | MAT-A | preview 后修改客户端响应，再提交不含 description 的入池请求 | 后端重新解析并冻结真实 desc；请求 hash 含 desc；伪造值无效 | P0 | 待执行 |
| F03 | intake -> pool | intake prepare 完成 | 完成制作并读取 recurring pool | pool description 与 intake 完全一致，非重新查询值 | P0 | 待执行 |
| F04 | pool -> queue | ready pool，隔离 DB | 仅调用 store/service 的排队单元路径，不触发 runner | queue description 与 pool 一致；caption 用该值渲染 | P0 | 待执行 |
| F05 | 源库变化不漂移 | 已有 intake/pool/queue | 修改测试库源 desc 后重读/重放 | 三层冻结值和最终 caption 均不变；新 intake 才可取新值 | P0 | 待执行 |
| F06 | 幂等与冲突 | 同 idempotency key | 相同冻结数据重放；再用不同 desc/模板重放 | 相同请求返回同记录；不同事实数据返回 409，不覆盖旧值 | P0 | 待执行 |
| F07 | 老库 additive migration | DB-OLD 备份 | 启动迁移并检查 schema/数据 | 新列存在且不丢历史；已发布 queue 不变；含 `{desc}` 且空值的 available pool 被阻断待回填 | P0 | 待执行 |

## U. TT 短链

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| U01 | 19 位保留命名空间 | 多个测试 queue identity | 生成短链 ID | 每个 ID 为 `8[0-9]{18}` 且唯一、稳定、不落入 X 既有空间 | P0 | 待执行 |
| U02 | W2A 参数合同 | 完整冻结元数据 | 生成 long URL 并解析 base/query 顺序和值 | base 为 `https://www.dramawavew2a.com/ads/101/2250/view`；顺序为 c、af_adset、af_adset_id、af_ad、af_ad_id、af_channel、af_c_id、af_dp；`af_channel=AIpost` | P0 | 待执行 |
| U03 | wrapper 原子/幂等 | 空短链目录 | 同身份生成两次，再尝试同 ID 不同目标 | 首次原子落盘；第二次复用；冲突返回 409，文件不被覆盖 | P0 | 待执行 |
| U04 | 缺失元数据 | 去掉 material_name/language/tag 等必要值 | 创建含 `{url}` 的 queue | fail closed，不生成猜测链接或半成品 wrapper | P0 | 待执行 |
| U05 | Nginx 精确路由 | 部署到隔离 Nginx | 请求 URL-TT，检查 no-cache/headers/Location/内容 | 命中 TT wrapper，状态和安全 header 符合合同，不命中 X handler | P1 | 待执行 |
| U06 | X 兼容性 | URL-X | 在同一 Nginx 配置请求旧 X 链接 | 结果与变更前一致；TT 路由未吞掉 X URL | P0 | 待执行 |

## D. `direct_outro` 与媒体合同

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| D01 | direct_outro prepare | GPU 隔离配置、OUTRO-V1、JOB-NEW | 只调用 `/internal/tt-post/prepare` | 成片 ready，复用既有审核的 Logo/tutorial-outro 和 phone-match 合成合同；未调用 publish | P0 | 待执行 |
| D02 | 时长公式 | 已知源/trim/outro/transition 时长 | ffprobe 源、片尾、成片 | 成片时长符合 `source-trim+outro-overlap` 及既有容差 | P0 | 待执行 |
| D03 | manifest/health 合同 | D01 完成 | 读取 health、响应和脱敏 manifest | mode/profile/outro 与 logo SHA/size/source SHA/size/trim/transition 齐全，`direct_post_eligible=true` | P0 | 待执行 |
| D04 | 成片 URL 身份 | D01 完成 | 比较 source/output URL 与 host，下载核 hash | URL 不同；output 属于 TT 专用 COS；hash/size 与响应一致；相同 URL 会被拒绝 | P0 | 待执行 |
| D05 | direct_clean 不变 | 切到既有 direct_clean 测试配置 | 运行原有 prepare 单测/命令捕获 | 仍只有源规范化命令，无片尾/logo/filter_complex，profile 和 eligibility 不变 | P0 | 待执行 |
| D06 | branded_preview 不变 | 切到既有 preview 测试配置 | 运行原有 prepare/health 测试 | 仍包含预览品牌流程，`direct_post_eligible=false`、review required | P0 | 待执行 |
| D07 | 复用契约漂移 | 同 job ID 分别改变片尾、profile、mode、source fingerprint | 重复 prepare | 每种变化均 409 idempotency conflict；完全相同契约才复用 | P0 | 待执行 |
| D08 | 无发布调用证明 | 记录 HTTP access/audit 与 ledger 基线 | 执行 D01-D07 后比对 | publish/canary 调用数为 0，publish ledger 与真实 Post 数无变化 | P0 | 待执行 |

## I. UI 与控制面回归

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| I01 | X 风格模板 UI | 打开 TT 素材池 | 查看宏帮助、caption 编辑和素材卡 | 明确列出精确 `{desc}`/`{url}`；每条素材显示真实渲染预览和 UTF-16 计数 | P1 | 待执行 |
| I02 | 逐素材长度/错误 | 同批选择 MAT-A/MAT-B/MAT-C | 输入同一模板并校验 | 每条独立判断；一条失败不会显示为全批通过，错误指出素材 ID | P0 | 待执行 |
| I03 | 请求体信任边界 | 拦截入池请求 | 检查 POST JSON 并尝试注入 description | 正常请求无 description；未知 description 字段被后端拒绝 | P0 | 待执行 |
| I04 | 自动排期可关闭 | 无效/降级账号也可见 | 只切关闭并保存 | 请求只依赖 account/enabled=false/expected_version；不要求时间、consent、creator-info；保留原时间/历史 | P0 | 待执行 |
| I05 | dirty draft/409 race | 两个页面加载同版本 | A 保存后 B 保存旧版本 | B 得到 409 且草稿不被后台刷新覆盖；可显式刷新重试 | P1 | 待执行 |
| I06 | run-now ready gate | 只有 preparing 素材，再增加 ready 素材 | 观察按钮和调用保护 | preparing 时不能立即发布；只有 ready+available 且 gate 满足才可用 | P0 | 待执行 |
| I07 | 降级账号与计数 | 账号源降级、含 preparing/ready 数据 | 加载页面 | 显示管理占位和警告；available 与 preparing 数量分开且准确 | P1 | 待执行 |

## S. 安全与回滚

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | prepare-only 审计 | 已保存基线 | 执行完整验收并汇总 access/audit | 只出现 health/prepare/read；无 publish/canary/run-now/schedule-save | P0 | 待执行 |
| S02 | 外部状态不变 | queue/ledger/schedule/Post 基线 | 验收后逐项读取 | queue/publish ledger/真实 Post 数量及 schedule version/enabled 均不变 | P0 | 待执行 |
| S03 | CPU 宏/短链回滚 | DB 与短链目录备份 | 在隔离副本回退 CPU 代码/config，再读旧数据 | 服务可启动；旧 queue/短链可读；新增列保留；不删除历史 wrapper | P1 | 待执行 |
| S04 | GPU profile 回滚 | 保留旧 direct_clean config/服务制品 | 回退 GPU mode/profile 并 health/prepare 一条隔离 clean job | health 恢复旧 profile，clean prepare 正常；无 Post/排期副作用 | P1 | 待执行 |

## 回归范围

- 2c：`disable_daily_schedule` 原子关闭、无伪 consent、保留时间/历史；无效账号管理占位；dirty draft 与 409；available/preparing 分计数；run-now ready gate；账号源降级警告。
- 旧 caption：两个 Drama ID 别名、无 URL/desc 模板、历史 queue 回读、幂等重放。
- 旧媒体：`direct_clean` 无片尾；`branded_preview` 非正式直发；现有 creator-info/publish gate 不被 mode 绕过。
- 短链：既有 X `s2l` 路由与历史 wrapper 不变。
- 存储：TT 结果只进 TT 专用 COS；其他业务配置与对象路径不变。

## 通过门槛

- 42/42 通过；P0/P1 开放缺陷为 0。
- 所有失败均有稳定错误码，并证明没有消费素材、创建 queue/Post 或修改 schedule。
- prepare-only 的媒体、manifest、COS、HTTP 审计和外部状态基线证据齐全。
