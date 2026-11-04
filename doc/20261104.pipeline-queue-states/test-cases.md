# 测试用例

- queued/ready 到期进入待执行；未来 1 秒及未来 1 小时进入预约中。
- running/legacy 正常进入执行中；failed 按选定时间、unknown 不受时间窗口排除。
- paused 与 metadata 不混计；四类无重复。
- 其他用户持有的正常锁仅等待；unknown/quarantine 锁形成异常，只暴露计数。
- 近期 3 次过期 + 新鲜 idle/selection -> 异常；render、旧过期事件不误报。
- 旧心跳但有效租约仍执行中；过期租约加失联显示异常。
- 查询前后 SQLite dump 一致；屏蔽 socket 仍能正常查询。
- 有索引查活跃记录；超过限制返回 null + 提示。
- Chromium 桌面/390px，四类标题数字与异常原因；旧 DTO 显示 —；保持账号权限、容量表单、15 秒刷新、旧深链。
