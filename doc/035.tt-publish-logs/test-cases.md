# 测试用例

## 测试范围

统一接口来源映射、筛选、统计、分页、安全字段、页面结构、旧页面移除、代理契约和既有 TT 回归。

## 测试数据

使用临时旧/新 SQLite 数据库，覆盖排期、立即测试、模板定时、模板手动、成功、失败、未知结果和无候选素材。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 两来源统一展示 | 两库均有任务 | 请求默认日志 | 按任务时间全局倒序，来源字段正确 | P0 | 通过 |
| TC-002 | 来源筛选 | 两库均有任务 | 分别筛选两种来源 | 只返回对应账本任务 | P0 | 通过 |
| TC-003 | 触发方式筛选 | 四种触发均存在 | 逐项筛选 | 标签和记录一一对应 | P0 | 通过 |
| TC-004 | 跨来源分页 | 两来源时间交错 | 请求连续分页 | 无重复、无遗漏、总数准确 | P0 | 通过 |
| TC-005 | 状态统计 | 覆盖关键状态组 | 请求日志 | summary 与过滤结果一致 | P0 | 通过 |
| TC-006 | 账号/素材/剧/模板筛选 | 建立不同属性任务 | 逐项筛选 | 精确命中 | P1 | 通过 |
| TC-007 | 日期边界 | 跨上海自然日 | 设置 from/to | 使用半开区间且边界正确 | P1 | 通过 |
| TC-008 | 安全响应 | 任务含内部媒体和 claim 信息 | 请求日志 | 响应不含敏感字段/URL | P0 | 通过 |
| TC-009 | 页面入口 | 已登录且有 tt_posts 权限 | 打开新页面 | 导航、筛选、表格和详情可用 | P0 | 通过 |
| TC-010 | 发布池瘦身 | 打开旧发布池 | 检查 DOM 与网络契约 | 无日志区且不请求 `/tasks` | P0 | 通过 |
| TC-011 | 素材池操作 | 可取消/待核对任务 | 检查页面操作路由契约 | 继续调用原接口且刷新列表 | P0 | 通过（契约） |
| TC-012 | 自动任务详情 | 有自动运行 | 打开详情 | 展示模板、任务和事件，不泄漏媒体 URL | P1 | 通过 |
| TC-013 | 单账本不可用 | 移除任一临时数据库 | 请求全部来源 | 失败关闭；单独查询可用来源仍成功 | P1 | 通过 |
| TC-014 | 4 位码展示 | 两类来源均有共享路由 | 打开列表和详情 | 仅显示合法大写 4 位码；不从 caption 推断 | P0 | 通过 |
| TC-015 | 自动 code 补读 | 自动任务有高位共享路由 | 查询自动日志 | 返回路由真实 code；共享路由缺失时留空且列表可用 | P0 | 通过 |
| TC-016 | 回填 dry-run | 有历史 published 空 code 排期及 direct test | 默认执行脚本 | 只发现排期，数据库字节、route 和 direct test 不变 | P0 | 通过 |
| TC-017 | 回填 apply 门禁 | exact plan 已确认 | 带 queue IDs、count、hash、backup apply | 先生成可校验备份，再原子分配 code；公共 resolver 精确命中 | P0 | 通过 |
| TC-018 | 回填异常关闭 | 计划变化、long URL 缺失或 route 冲突 | 尝试 apply | 整批回滚，不写半批数据 | P0 | 通过 |
| TC-019 | 空长链逐 ID 授权 | 同批包含 frozen long URL 与 legacy 空 long URL | exact dry-run/apply | reconstruction ID 必须精确等于空长链候选；漏选、多选、无显式 queue scope 均失败 | P0 | 通过 |
| TC-020 | ledger-only 重建 | 唯一 consumed recurring、唯一 publish_reconciled 事件、冻结账号快照完整 | 重建 route | 身份与 publish ID 精确一致；使用 queue.created_at surrogate 和已记录 fallback；不读 caption/当前 resolver | P0 | 通过 |
| TC-021 | ledger 证据漂移 | recurring/event/snapshot 在 dry-run 后变化 | 使用旧 hash apply | plan hash 变化，事务零写入，direct test 不变 | P0 | 通过 |
| TC-022 | 混合原子回填 | 一条 ledger route 与一条 frozen URL route | 同批 apply | 两条均生成唯一 code 并精确解析；原 queue/recurring/event/长短链不变 | P0 | 通过 |

## 回归范围

- TT Post 发布池素材入池、预制作、排期/立即测试入口。
- TT 自动发布模板列表、编辑、运行记录和手动执行。
- 主 API cookie 导航权限和 sidecar 路由白名单。
