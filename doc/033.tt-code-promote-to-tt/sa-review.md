# SA 评审意见

## 结论

通过。采用“单 Nginx alias 切换、双入口共用同一静态页面”的最小方案；无需复制静态文件，也无需修改发布服务或 Featured 资源链路。

## 问题清单

| 编号 | 级别 | 问题 | 处理 | 状态 |
| --- | --- | --- | --- | --- |
| SA-001 | P0 | 两份配置重复声明 exact `/tt` 会导致 `nginx -t` 失败 | `/tt` 只保留在 `tt-drama-search.conf` | 已采纳 |
| SA-002 | P0 | 全量同步旧 JS 会覆盖线上独立 W2A 路径差异 | 仅部署变更后的 Nginx 配置，旧静态文件零覆盖 | 已采纳 |
| SA-003 | P1 | 删除 v1 API/旧 JS 会使快速回滚失效 | 原路由与静态文件保留 | 已采纳 |
| SA-004 | P1 | `/tt-code` 重定向可能破坏书签或产生额外跳转 | 保持 200 同页兼容入口 | 已采纳 |
| SA-005 | P1 | 只测宏渲染函数不足以证明正式发布 payload | 增加自动排期到 GPU caption 及 GPUClient `title` 回归 | 已采纳 |
| SA-006 | P1 | 新模板不会回写历史素材池 | 明确冻结语义，上线前只读检查生产池中宏覆盖情况 | 已采纳 |

## 决策记录

- 路由切换不需要切换资源 release symlink，也不需要等待后再改 Featured 资源；仍应避开正在执行的 oneshot 并复核其自然运行结果。
- 线上部署只替换 `/etc/nginx/default.d/tt-drama-search.conf`，先备份、再 `nginx -t`、最后 reload。
- `/opt/tt-post/current`、`tt-post-service`、Redis 和 `drama-material-api` 均保持不动。
- 不以真实 TikTok 发帖作为测试手段；用事务、队列、HTTP payload 与 GPU worker 合同测试闭环证明。
