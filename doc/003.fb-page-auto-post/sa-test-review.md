# SA 测试用例评审

## 结论

通过。本地覆盖最危险的重复发布与权限边界；真实 SQL canary 和 Graph status 枚举验证为部署前门禁。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-01 | Graph ID | 原设计可能把ID当完成 | 增加 submitted/reconcile | 已关闭 |
| STR-02 | 千Page池 | 未证明候选只查一次 | 加调用计数断言 | 已关闭 |
| STR-03 | 跨组Page | 组互斥不足 | lineage+唯一索引 | 已覆盖/部署前演练 |
| STR-04 | 真实MySQL | Fake不能发现列/索引/SQL mode | 部署前EXPLAIN+只读canary | 待部署 |
| STR-05 | GPU峰值 | 无NVENC耗时基准 | 默认20 jobs/slot fail closed，开gate前实测 | 待部署 |
| STR-06 | prepare-only边界 | 复用完整TT worker会暴露发布面 | 独立fb_gpu入口，只开放health/prepare | 已关闭 |

## QA 修订确认

已补 Graph、性能、重叠和旧基线回归。
