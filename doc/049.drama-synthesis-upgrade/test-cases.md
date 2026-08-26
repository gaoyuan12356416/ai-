# 测试用例

## 原则

测试只使用临时 SQLite、虚假目录和 fake YouTube client；不得访问真实 Google API、发布视频或评论。HK/CPU/CloudFront 只允许独立 QA 做只读或明确授权的 canary。

## 用例列表

| 编号 | 场景 | 预期结果 | 优先级 | 实现自测 |
| --- | --- | --- | --- | --- |
| DS-001 | 四个输出 checkbox 初始态 | 全部未勾选 | P0 | PASS |
| DS-002 | 零输出请求 | UI 与 backend 均拒绝 | P0 | PASS |
| DS-003 | 自动随机配方重复解析 | 同任务结果完全相同且四层齐全 | P0 | PASS |
| DS-004 | 手动模式缺层/未知素材 | 拒绝；合法四层冻结 | P0 | PASS |
| DS-005 | 同 job 重试冻结/冲突 | 同配方复用，不同配方冲突 | P0 | PASS |
| DS-006 | GPU result identity | output SHA/profile/recipe SHA 完整 | P0 | PASS |
| DS-007 | 短链精确目标和 HTML | 参数顺序正确，无开放跳转脚本 | P0 | PASS |
| DS-008 | 相同 job 重建短链 | ID/URL/文件内容幂等；目标不可变 | P0 | PASS |
| DS-009 | publisher 未配置 | 503/failed，不返回伪成功 | P0 | PASS |
| DS-010 | 频道 eligibility | 仅 status=1+refresh+client+upload scope | P0 | PASS |
| DS-011 | 评论 scope | 无 `youtube.force-ssl` 时拒绝/禁用 | P0 | PASS |
| DS-012 | operation_id 重放 | 返回同一任务，不重复视频 | P0 | PASS |
| DS-013 | 视频成功再评论 | 视频/评论分别记录 published | P0 | PASS |
| DS-014 | 308/中断后重试 | 查询已存 session，不新建/重复上传 | P0 | PASS |
| DS-015 | 上传结果未知 | unknown，后续不自动替代发布 | P0 | PASS |
| DS-016 | 已成功后再次发布 | 首次 409，二次确认后才入队 | P0 | PASS |
| DS-017 | 404 expired session | unknown/fail closed | P0 | PASS |
| DS-018 | worker crash lease recovery | 到期任务可被重领并继续状态机 | P0 | PASS |
| DS-018A | 评论调用中 worker crash | 标记 unknown，禁止自动重复评论 | P0 | PASS |
| DS-019 | HK v1 清单 | 20/20、520297533 bytes、SHA 完全一致 | P0 | 待部署前只读演练 |
| DS-020 | 线上旧三产物/侧栏/表格回归 | 合同保持不变 | P0 | 待独立 QA 浏览器回归 |
| DS-021 | 不含新移除字段 | 新 payload 无 cover template/naming rule | P1 | PASS |
| DS-022 | credential/secret 泄漏扫描 | API/diff/log 无秘密值 | P0 | 待最终 gate |
| DS-023 | YouTube live schema contract | SQL 使用 `ch.channel_status`，禁止 `ch.status` | P0 | PASS |
| DS-024 | raw legacy job 的随机模板 YouTube 来源 | 从已完成冻结 recipe/output 解析 URL | P0 | PASS |
| DS-025 | SQL 标识对抗输入 | quote/backslash/越界 ID 在查询前失败关闭 | P0 | PASS |
| DS-026 | 评论请求前临时失败 | 首次不发评论，known-safe retry 恰好发布一次 | P0 | PASS |

## 回归范围

`/api/drama-material/products`、job list/detail/create/retry/delete、原三类产物、Feishu 权限、GPU legacy 18787、W2A/TT resolver、ad-material demand review live delta。
