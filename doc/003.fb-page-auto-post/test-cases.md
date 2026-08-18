# 测试用例

## 测试范围

输入、只读源、SQLite 状态机、Token failover、unknown/submitted 对账、权限导航和旧功能回归。

## 测试数据

临时 SQLite、Fake MySQL、Fake Graph；不连接生产、不调用 Meta。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-01 | 视频/data_source | Dramawave合法模板 | 校验 | video、服务端冻结6 | P0 | 通过 |
| TC-02 | 非法宏 | 未知/缺括号 | 保存 | 400 | P1 | 通过 |
| TC-03 | 随机窗口不足 | 3次/1小时 | 保存 | 拒绝 | P1 | 通过 |
| TC-04 | 负责人列 | Fake SQL | 列组 | 使用 g.user_id | P0 | 通过 |
| TC-05 | 跨组重复Page | 同Page两组 | 列Page | 合并并保留lineage | P0 | 通过 |
| TC-06 | Token去重 | 相同Token不同用户 | 解析 | 内存按Token去重 | P0 | 通过 |
| TC-07 | 单次素材扫描 | 2 Page | 冻结 | candidates一次 | P0 | 通过 |
| TC-08 | 缺Token | 1 Page缺Token | 冻结 | 保留跳过原因 | P0 | 通过 |
| TC-09 | 前序积压 | 已有queued run | 新时隙 | 409不叠加 | P0 | 通过 |
| TC-10 | 随机计划 | 同日读取两次 | 比较 | 不重抽、非整点、间隔60分 | P1 | 通过 |
| TC-11 | 明确失败 | 首Token拒绝 | 执行 | 第二Token提交 | P0 | 通过 |
| TC-12 | 网络unknown | 首Token断连 | 执行 | 不调用第二Token | P0 | 通过 |
| TC-13 | Graph ID | 返回ID | 完成 | submitted非published | P0 | 通过 |
| TC-14 | 对账ready | submitted | reconcile | published且无POST | P0 | 通过 |
| TC-15 | live gate | gate=0 | 执行 | 零Graph调用 | P0 | 通过 |
| TC-16 | 权限导航 | 静态契约 | 检查 | Cookie/模块/共享壳完整 | P0 | 通过 |
| TC-17 | X/TT基线 | 新增app路由 | 66项回归 | 全通过 | P0 | 通过 |
| TC-18 | 关闭门禁tick | gate=0且有due模板 | tick | 只清理，不读取due/建run | P1 | 通过 |
| TC-19 | 启用模板编辑 | 模板enabled | 更新配置 | 409，要求先停用 | P1 | 通过 |
| TC-20 | 成员漂移冲突 | 两个已启用不同组后来出现同Page | 自动冻结 | 409，不创建双队列 | P1 | 通过 |
| TC-21 | 完整日随机24次 | 00:00-23:59 | 生成计划 | 必得24个非整点且相邻至少60分 | P1 | 通过 |
| TC-22 | 对账凭证轮换 | 首Token明确凭证失败、次Tokenready | reconcile | 随机不放回后published | P1 | 通过 |
| TC-23 | 对账全部凭证失败 | 所有健康Token均明确拒绝 | reconcile | unknown/attention，保留Graph ID | P1 | 通过 |
| TC-24 | 对账无法判定 | 首Token网络/不明确响应 | reconcile | 保持submitted，不换Token、不POST | P1 | 通过 |
| TC-25 | 视频处理失败 | Graph明确processing_failed | reconcile | failed_without_retry，不换Token | P1 | 通过 |
| TC-26 | Tick重入 | 首次tick仍运行 | 并发第二次tick | 返回already_running且不重复冻结 | P1 | 通过 |
| TC-27 | 多模板tick/超时 | 同分钟5个模板 | tick及unit契约 | scheduler快速入队，重活由独立有界unit处理 | P1 | 通过 |
| TC-28 | 素材Top排序 | 高ID短剧指标更优 | 候选查询 | 条件先于LIMIT，drama主/material次，高ID仍入Top | P1 | 通过 |
| TC-29 | 组类型闭集 | Page组查询 | 检查SQL | list_groups/list_pages均限定type IN (0,1) | P2 | 通过 |
| TC-30 | 对账审计保留 | 发布前2次明确失败后submitted | reconcile成功 | ledger definite_attempts仍为2 | P2 | 通过 |
| TC-31 | 视频模板缺失/非法 | 无字段、空值、其他枚举 | 保存 | 409 `fb_auto_video_template_required` | P0 | 通过 |
| TC-32 | 指标完整日 | 两个READY自然日 | 加载窗口 | 冻结generation IDs，ratio-of-sums | P0 | 通过 |
| TC-33 | 指标缺日 | 窗口缺任一天 | 选择 | fail closed，不查窗口MySQL | P0 | 通过 |
| TC-34 | 指标原子指针 | 新代次存在非法行 | 刷新 | 旧active pointer不动 | P0 | 通过 |
| TC-35 | 指标SQL分组/排序 | ONLY_FULL_GROUP_BY；material 1/10/2及超大ID | 检查单日SQL合同 | 排序表达式全部分组；按长度+字符串得到任意精度数值序 | P0 | 通过 |
| TC-35A | 单日SQL | 刷新昨天 | 检查SQL | product+platform+dt等值，无窗口扫描 | P0 | 通过 |
| TC-36 | future prepare | 当前10:00、发布10:30、ahead 4h | tick | due slot提前入队，重复tick为0 | P0 | 通过 |
| TC-37 | 调度重启 | watermark落后8h | tick | 有界catch-up并记录missed | P1 | 通过 |
| TC-38 | gate0 | scheduler/run-now/prepare/Graph | 执行 | 不写slot/run，不调用外部服务 | P0 | 通过 |
| TC-39 | GPU严格响应 | job/content/profile/url/hash/size/duration | prepare | 全部匹配才ready | P0 | 通过 |
| TC-40 | GPU源URL回退 | output=source | prepare | failed，不调用Graph | P0 | 通过 |
| TC-41 | prepared URL发布 | source与prepared不同 | Graph执行 | 只使用prepared URL | P0 | 通过 |
| TC-42 | 容量门禁 | Page×频率超过env上限 | 启用 | 中文实数/上限，409 | P1 | 通过 |
| TC-43 | 全局同槽容量 | 两个模板各15 Page、上限20 | 启用/运行 | 按全局30拒绝 | P0 | 通过 |
| TC-44 | disable/version竞态 | catalog期间停用或换版 | 最终事务 | 不创建run | P0 | 通过 |
| TC-45 | Page增长竞态 | 初读1、冻结后21、同槽上限20 | 运行复核 | 409不创建 | P0 | 通过 |
| TC-46 | due版本并存 | v1已有future slot后更新v2 | scheduler重扫 | v1/v2各自幂等slot | P0 | 通过 |
| TC-47 | 两个未来时隙 | 同Page间隔60分钟 | plan/prepare | 两个ready可提前并存 | P0 | 通过 |
| TC-48 | Page发布互斥 | 首时隙submitted，次时隙ready到期 | claim | 不抛唯一冲突；首个published后才领取次个 | P0 | 通过 |
| TC-49 | reconcile退避 | submitted/transient | 连续claim | 5分钟内不可重复领取 | P0 | 通过 |
| TC-50 | prepare退避 | GPU/COS 502 | 连续claim | planned+未来时间；到期稳定job重试 | P0 | 通过 |
| TC-51 | Graph原子接受 | accepted写入中注入异常 | 事务检查 | attempt/task/ledger全部回滚 | P0 | 通过 |
| TC-52 | 指标库隔离 | metric长写并发operational写 | 创建模板 | 不互锁；相同路径启动失败 | P0 | 通过 |
| TC-53 | 旧指标代重试 | active已从A切B | 重试A | pointer仍为B | P0 | 通过 |
| TC-54 | 语言规范化 | english与zh-tw | 保存/SQL | 分别冻结en/zh-tw | P1 | 通过 |
| TC-55 | 手动执行异步幂等 | 相同operation_id重复请求 | run-now | 202且只有一条manual due-slot | P0 | 通过 |
| TC-56 | 失败目录清理 | 新鲜/旧失败/成功manifest/链接 | cleanup | 只删安全根下过期失败job | P1 | 通过 |
| TC-57 | future素材原子预留 | cooldown=0、两个并发future planner、同Page两候选 | 同时创建 | 两个run分别预留501/502 | P0 | 通过 |
| TC-58 | 8 Token时间预算 | 前7个每次121秒明确失败、第8个成功 | execute/reconcile | 968秒内落账，租约未过期且无重复claim | P0 | 通过 |

## 回归范围

`scripts.test_x_accounts_app_contract`、`scripts.test_tt_auto_publish_app_contract`、`scripts.test_x_auto_publish_app_contract`。
