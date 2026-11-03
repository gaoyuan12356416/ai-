# SA 代码评审

独立 planner_review、receipt_reconcile、prepost_recovery 分别复核冻结身份/计划写入、成功回执与节点上线、两节点 outbox 与准备/上传证据。root 审核提交差异并串行执行生产业务状态恢复。独立评审不是授权任何未知结果重发。

## 关键结论

- 成功回执校正必须同时匹配队列、账号、唯一 Post ID；不因 UI 显示 failed 生成新任务。
- pre-Post 授权记录精确 source/stage/from-generation/to-generation、旧 attempt、冻结队列/日志/媒体哈希。授权实际消费发生在下一次 initialize，保留原历史并只增加一次 attempt。
- 所有旧 generation 回调仍受中央及 native fencing 约束。没有将任意 failed/unknown 批量改为 queued，也没有把原 attempt 清零。
- 只读网络重试只覆盖有限的 HTTP 5xx/网络错误，明确 4xx、身份、素材大小/哈希、lease 和本地磁盘错误仍立即失败；Post/Repost 仍单次请求。
- 短剧 selector 必须排除所有历史状态已占用的 episode_key，不能通过删除 cancelled 记录或推进绑定游标避开重复约束。1029/1031 helper 要求实际已部署文件与精确 Git blob 一致。
- 增量运行时补丁基于当前树核验，只覆盖 owning X runtime；保留其他线程的 GPU/YouTube 代码、容量及配置。GPU 候选验证通过后仍需等新 PID 和实际 cmdline 才能确认上线。

## 已发现并处理的问题

| 问题 | 处理 | 状态 |
| --- | --- | --- |
| runtime staging 目录权限使服务用户不能遍历 | 公共 runtime 根可遍历，继承原私有文件权限；operator audit 仍私有 | 已修复并部署 |
| 临时本地端口拦截阻断自身健康检查 | 精确豁免服务用户健康请求，清理旧规则并按实际 PID/目录核对 | 第一轮已纠正 |
| 选材期间可用账号集合变化使 FIFO proof 失效 | 写计划前重核原账号全集，仅有限次用原快照重建 proof | 已部署 runner1212323 |
| 其他维护流程将 unknown5452 重置 | 保存原始证据并恢复中央 unknown hold，不覆写历史 | 已隔离 |
| 后续失败未在最初恢复清单内 | 以精确新计划另行核验，不能复用旧清单或旧授权 | 持续核对 |
| manual1390/1401 虽自身零队列但账号有另一个 unknown | 保留账号级保护，不增加二次绕过恢复入口 | 已保留 |

## 验证

相关专项、集成、编译和差异检查见 test-report.md。当前第二轮 CPU/X 部署未完成前，结论限定为代码测试和部分运行时验收通过；不报告全部积压完成。
