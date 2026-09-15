# 测试用例

1. 三个独立Video：首个失败，另外两个仍发送DELETE，run=partial。
2. 三个独立Video全部成功，全部发送且run=completed。
3. 首个unknown仍继续其他对象；后续执行不重发unknown/成功项。
4. 真Graph模拟首个invalid video ID，后续完整账户库无此ID，持久化already_deleted、原错误、核实证明及pair回执；后续一个成功一个失败，重试仅失败项。
5. invalid ID后GET仍在库/权限不足/超时/分页异常/到达预算，保留失败，不伪造删除成功。
6. 泛100、权限错误、DELETE超时/5xx不触发已删除猜测；后两者仍unknown。
7. 同一prepared Token贯穿DELETE及后置GET，无二次provider查找、轮换或重复DELETE；日志/台账无Token。
8. 旧单Video、多账户、当前权限撤销、台账失败、重启锁、分阶段、前端既有契约回归。
9. 回滚同时暂停账户Video和旧节点Video写入口；Ad仍可执行。原代码完整保留，已确认的回滚hash可reapply。
