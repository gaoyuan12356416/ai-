# 测试用例

## 范围与数据

使用临时 SQLite、模拟 X/GPU 客户端和本地 HTTP Server；不创建真实测试 Post/Repost。生产核对仅访问原队列、恢复清单和受控备份副本。

| 编号 | 场景 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| TC-01 | 首轮配置账号存在 unknown/明确 locked | 只跳过对应账号，完整配置身份和历史日志保留，正常账号可建队/发布 | P0 | PASS |
| TC-02 | 第二轮账号阻塞、身份/语言或容量漂移 | 拒绝本轮，不临时换账号或用过期容量证据 | P0 | PASS |
| TC-03 | 实际冻结队列或 relay 有未知结果 | 最终 fence 阻止 X 写；配置中未建队账号不误伤该批次 | P0 | PASS |
| TC-04 | 目标403、source403与后续确认成功 | 锁归属实际动作账号；profile成功不解锁；confirmed相应动作才解除历史锁证据 | P0 | PASS |
| TC-05 | healthy子集选择素材、短剧、relay | FIFO容量按eligible计算，foreign_owner按configured计算，保留未完剧绑定 | P0 | PASS |
| TC-06 | 只读短剧needs_review与创建计划业务拒绝 | 全池保护保留；精确业务码，不改成计划写入结果未知 | P0 | PASS |
| TC-07 | 创建计划连接丢失/未知异常 | 仍是unknown，禁止再次创建计划 | P0 | PASS |
| TC-08 | deferred短剧正常/可修复媒体，direct/relay | 源文件一次下载、最多一次修复、保留原URL和路由，通过SHA/size/probe才进入X | P0 | PASS |
| TC-09 | 修复禁用、超时、SHA/策略/格式错误、文件替换 | 准备失败attempt0，unknown0；不递归修复、不触发X | P0 | PASS |
| TC-10 | 长准备期间Token过期/会员或语言变化 | credentials外刷新并复验source/target；上传前复验当前会员和文件 | P0 | PASS |
| TC-11 | published/unknown/attempted日志再次进入 | 在准备前停止，不重下源、不重发 | P0 | PASS |
| TC-12 | 历史恢复源ID/URL漂移 | 媒体修复前拒绝，原清单/绑定/队列不修改 | P0 | PASS |
| TC-13 | schedule timeout与请求参数 | schedule最大7200、默认900；Daily边界不变；非bool的schedule_preflight拒绝 | P1 | PASS |
| TC-14 | 全量错误码与中文说明同步 | 新媒体模块纳入扫描，新增稳定码均有运营说明 | P1 | PASS |
| TC-15 | 生产备份、16条完整恢复清单、保留3条人工记录 | quick_check ok/FK0，清单严格0attempt无ID，副本apply和live apply分开验证 | P0 | 备份/零写快照PASS，apply待执行 |
| TC-16 | 部署与自然定时发布 | exact GitHub commit、health/hash通过；实际发布结果以ledger核对，不用测试帖代验 | P0 | 待执行 |

## 回归命令

`python -m unittest discover -s scripts -p 'test_x*.py' -q`。Windows部分POSIX特有用例允许跳过，生产Linux必须重新运行。
