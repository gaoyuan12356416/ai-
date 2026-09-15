# QA用例

| 场景 | 预期 |
| --- | --- |
| Video.from指向已授权Page/候选上传User | 选择匹配身份 |
| from缺失，Creative精确包含Video | 使用该Creative关联PageToken |
| Creative引用其他Video | 不借用该Page身份，不修改删除目标 |
| 无法读Video/Creative或Page查询异常 | 原UserToken单次DELETE |
| 只有历史Page关联、UserToken缺失 | 当前PageToken仍可选择 |
| 相同用户集下两个Page | 凭证不串用 |
| Page授权状态/Token发生变化 | 下一次执行重新读取 |
| 内部user_id与Meta用户ID | 通过可靠JOIN映射并分别记录 |
| PageToken明确失败、超时 | 不自动换Token或重放 |
| 预写审计失败/中断 | 不发新DELETE，保留unknown锁和所选身份 |
| 成功/unknown/重复请求/ACL/冻结IDs | 保持既有约束 |
| 诊断字段/不存在字段/XSS | 安全显示、无秘密、无额外开关 |
| 生产probe | GET-only transport；17项Page身份一致，任务台账不变 |
