# 开发计划与责任

采用隔离工作区 D:/codex/worktrees/meta-drama-asset-delete-20260915，分支 codex/meta-drama-asset-delete-20260915；基线 a5b12df 来源于当前线上文件。

- 主执行：业务匹配、Meta适配器、服务编排、HTTP/权限集成、导航、部署与文档。
- 台账工作流：独立store及事务/并发/重启测试；后续独立审查core/source/graph/service。
- 页面工作流：仅页面HTML/CSS/JS及本地Mock服务器，避免共享文件冲突。

完成顺序：需求/SA评审 → 接口/测试设计 → 台账、UI并行 → 编排与路由 → 单元/浏览器验收 → 代码评审 → GitHub提交 → 精确版本备份部署 → 只读生产验收。

上线仅变更app.py、功能包、该页面三文件、quick-nav.js、index.html；公共静态和服务static同时更新。主API窄重启前检查活跃发布任务，不主动创建发布或删除任务。
