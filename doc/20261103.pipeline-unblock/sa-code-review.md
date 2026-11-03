# 代码评审

2026-11-03 完成根代理与独立代理交叉审查。

- FB 恢复：精确两任务/Page/video allowlist，完整 Page 查询和二次对象 GET，文案/短链/时长/所属 Page/真实发布时间证据；双项事务 CAS、唯一视频归属、无平台写接口，原尝试不变。独立评审通过。
- TT 987：完整 207 次前置拒绝事件，与 GPU 无发布账本新鲜证据核对；无 publish_id/unknown/claim/operation 方可收尾。评审补齐 finished_at、素材账本与 run 正常终态元数据，同事务失败回滚；保留全部历史。
- 监控：纯静态兼容当前 DTO，不重启协调器或节点；保留生产 07692ce 已有制作/发布 ready 分阶段口径。
- TT readback：只有合法原 publish_id 的成功 pending 回查清旧错误，仍保留 pending/unknown 保护。异常或 ID 不符仍报错。
- 部署：原文件摘要、运行路径、挂载、无在途、自然停止、短门禁、门禁过期及独立清理、并发竞态均设守卫。只替换 tt_readback.py；容量、核心调度和执行器不改。
- Linux dry-run 发现 Graph +0000 在 Python 3.9 不兼容，本地修复、提交 GitHub 并在生产 Python 复验后才执行。
