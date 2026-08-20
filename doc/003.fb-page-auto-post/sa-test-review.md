# SA 测试用例评审

## 结论

通过。本地覆盖最危险的重复发布与权限边界；生产SQL、30日指标cache、Graph status只读枚举和prepare-only GPU/COS/NVENC均已完成closed-gate验收。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-01 | Graph ID | 原设计可能把ID当完成 | 增加 submitted/reconcile | 已关闭 |
| STR-02 | 千Page池 | 未证明候选只查一次 | 加调用计数断言 | 已关闭 |
| STR-03 | 跨组Page | 组互斥不足 | lineage+唯一索引 | 已覆盖；closed-gate账本为0 |
| STR-04 | 真实MySQL | Fake不能发现列/索引/SQL mode | EXPLAIN+只读canary+30日refresh | 已关闭 |
| STR-05 | GPU峰值 | 无NVENC耗时基准 | 默认20 jobs/slot fail closed，开gate前评审同槽连续吞吐 | 单任务已验；同槽吞吐为live前门禁 |
| STR-06 | prepare-only边界 | 复用完整TT worker会暴露发布面 | 独立fb_gpu入口，只开放health/prepare | 已关闭 |
| STR-07 | desc来源 | 容易误用`ads_drama_info`或跨语言描述 | 真实schema只读确认；Fake SQL锁定resource/app/content/language/type | 已通过 |
| STR-08 | 短链/Graph顺序 | 单测只验证文案会漏掉外部写顺序 | 注入writer失败并断言Page凭证与Graph均未调用 | 已通过 |
| STR-09 | 线上验收 | 创建模板会扩大生产状态 | gate=0、六表计数不变、404/方法/headers验收 | 部署后执行；禁止以模板验收 |

## QA 修订确认

已补 Graph、性能、重叠和旧基线回归。
