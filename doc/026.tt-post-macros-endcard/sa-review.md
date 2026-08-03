# SA 评审意见

## 结论

需求设计评审结论为**有条件通过**：方案可以进入实现和离线测试，但在 description 冻结链路、`direct_outro` 资产契约、UTF-16 fail-closed、历史数据处理和 prepare-only 安全证据全部完成前，不得部署和发布。

本评审只确认设计口径，不代表代码已经完成，也不代表真实 TikTok 发布已经验证。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | description 数据流 | 现状可在 resolver/intake 看到 description，但 recurring pool/queue 缺少稳定冻结链路，重试或排期可能丢值或重新取值 | 三层增加非空字段；仅 intake 从 `ads_drama_resource.desc` 冻结，后续只复制 | 已纳入需求，待代码验收 |
| SA-002 | P0 | caption renderer | 顺序调用普通 `replace` 容易把 desc 内 `{url}` 等文本二次解释 | 实现一次 token 扫描；精确小写、非递归，并加 desc 含宏样式文本测试 | 已纳入需求，待代码验收 |
| SA-003 | P0 | GPU media mode | `direct_clean` 正式可直发但无片尾，`branded_preview` 有片尾但不可直发，直接改任一模式都会破坏既有合同 | 新增独立 `direct_outro` 和 profile；两个旧模式不变 | 已纳入需求，待代码验收 |
| SA-004 | P0 | 验收边界 | 真实 Post 会产生不可逆外部影响，且本轮目标可通过制作证据完成 | 仅 prepare-only；发布、canary、run-now、schedule 写接口全部列为禁止项 | 已冻结 |
| SA-005 | P1 | 长度校验 | Python `len` 与 TikTok UTF-16 计数不等价，emoji 边界可能误判 | 统一 UTF-16 code unit helper；2200/2201 和 emoji 组合测试 | 已纳入需求，待代码验收 |
| SA-006 | P1 | 客户端信任边界 | 若 UI 提交 description，用户可伪造事实数据，preview 与写入也可能 TOCTOU | 写请求拒绝 description；入池时后端重新 resolver 并冻结 | 已纳入需求，待代码验收 |
| SA-007 | P1 | 老数据 | additive default `''` 可能让模板含 `{desc}` 的老记录发布空描述或字面宏 | 历史已发布不改；available 老记录必须回填重算或重新入池，空值 fail closed | 已纳入需求，部署前盘点 |
| SA-008 | P1 | prepare 复用 | 仅以 source URL hash 识别源内容时，可变 URL 会错误复用旧输出 | 冻结 source SHA/size；无法提供时要求不可变 URL，验收使用新 job ID | 已纳入风险，待最终实现决策 |
| SA-009 | P1 | 固定片尾/Logo | 只记录路径无法证明使用的是已审核资产 | manifest/reuse 冻结 outro 与 logo 的 SHA/size、transition、profile；部署记录资产版本 | 已纳入需求，待代码验收 |
| SA-010 | P1 | 短链路由 | TT 19 位 `8` 开头链接可能被 X 通用 `s2l` 路由先匹配 | TT 精确 regex 放前，并回归 TT/X 两类 URL | 已纳入需求，待 Nginx 验收 |
| SA-011 | P1 | 分支合并 | 2c UI 修复和宏分支同时修改 core/service/UI 及其测试；无文本冲突不等于无语义回归 | 对 schedule disable、状态计数、dirty draft、run-now gate 做专项语义评审和回归 | 已纳入测试矩阵 |
| SA-012 | P2 | 双轨发布 | CPU 宏/短链和 GPU mode 若绑在同一不可分部署，会扩大故障面 | 分别配置、验证和回滚；CPU/GPU profile 只在最终切换窗口对齐 | 已纳入部署方案 |

## 决策记录

| 决策 | 结论 | 理由 |
| --- | --- | --- |
| description 何时冻结 | material intake 创建时 | 首次后端确认事实数据，且可供异步制作、重试和排期复用 |
| description 来源 | 仅 `ads_drama_resource.desc` | 防止客户端伪造和多来源漂移 |
| 宏替换模型 | 单次、精确 token、非递归 | 消除注入式二次替换和大小写歧义 |
| TT 长度处理 | 2200 UTF-16，超限拒绝 | 平台限制应显式暴露，不能改变文案语义 |
| 片尾模式 | 新增 `direct_outro` | 维持 `direct_clean` 与 `branded_preview` 兼容性 |
| `direct_outro` 合成器 | 复用既有已审核的 Logo/tutorial-outro compositor，不增加新的视觉元素 | 保持当前片尾合同，并用独立 mode/profile 隔离正式直发资格 |
| 本轮发布验证 | prepare-only | 可验证媒体合同，同时避免创建真实外部内容 |
| 数据迁移 | additive columns，历史发布不回写 | 保持可回滚性和审计真实性 |

## PM 修订确认

- `requirements.md` 已明确 `{desc}` 完整冻结路径、单次替换和 UTF-16 fail-closed。
- 已把 `direct_outro` 与两个旧模式拆分，并给出独立 profile、资产指纹和 eligibility 约束。
- 已补充 prepare-only 禁止清单、验收前后基线和 CPU/GPU 双轨回滚。
- 已把 2c 自动排期/素材状态/UI 修复纳入回归。
- 当前仍处于“可开发、不可发布”状态。
