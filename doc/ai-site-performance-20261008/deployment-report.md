# 2026-10-08 AI 后台访问性能修复

修复已上线至 `43.166.187.96`。代码发布点为
`6acd1b493fe2cfcba534069993731643de925307`，分支
`codex/ai-site-performance-20261008`。本报告的后续文档提交不改变部署代码。
时间均为北京时间。

## 实测结果

| 检查项 | 修复前 | 修复后 |
| --- | --- | --- |
| 本机访问公网首页 | 8.8–17 秒，两次超过 20 秒 | 最后三次 1.632、1.562、1.602 秒 |
| 首页传输大小 | 288586 字节 | gzip 58835 字节，减少约 80% |
| 服务器本地首页 | 约 13 毫秒 | 约 5.8 毫秒 |
| 服务器本地 quick-nav.js | 未压缩 32439 字节 | gzip 6979 字节，约 0.8 毫秒 |
| TT 自动发布健康检查 | 错误判定不可用 | HTTP 200，ready=true，problems=[] |
| YouTube 素材关联查询 | MySQL 3024，超过 8 秒上限 | 100 条素材完整匹配，4 个关联查询均成功 |

这是本次操作位置的访问样本，并非所有地区的延迟保证。源站在美国
Ashburn，观察到连接 RTT 约 300–340 毫秒及较多重传；CPU、内存、磁盘
和本地接口没有资源耗尽迹象。源站位置没有改变。

## 已部署变更

1. Nginx 为 HTML、JS、CSS 等文本开启 gzip。公共 JS/CSS 缓存 5 分钟，
   明确使用内容哈希版本参数的资源可长期缓存。页面、鉴权、API 和 JSON
   保持原缓存边界；解压后的首页 SHA256 与发布前相同。
2. 验证内核支持及访问改善后，启用并持久化 BBR，保留原 fq_codel。
3. TT Featured 的当前发布目录补充 `tt-drama-featured` 用户 ACL。
   实际刷新成功：19 个语言文件更新、78 个缩略图更新、缩略图失败 0。
   使用现有推荐数据源，未重写推荐业务数据。
4. TT 健康检查兼容 systemd 的 month/year 运行时长格式，保留触发器停用
   和调度过期时的失败检查。修复后停用触发器仍返回 HTTP 503，恢复后
   返回 HTTP 200；同时恢复原本已启用但未运行的 runner.path。
5. YouTube 剧目元数据查询每次最多 20 个 content_id，跨批去重，保留
   全局 1000 条唯一关联记录上限及 ID、语言、歧义校验。生产查询继续
   经过 SQL Gate，未增加连接容量或 8 秒查询上限。

主 API 的线上目录是组合运行环境，只覆盖经过 SHA256 核对的
`features/youtube_auto_publish/drama.py`，没有整目录同步。TT 只更新
健康检查 helper。待活跃准备任务及上传租约结束后才重启相关服务。

## 验证与边界

- Windows：4 项 TT 时长测试、23 项 YouTube 关联测试、6 项素材选择
  测试通过。Linux：4 项 TT、23 项 YouTube 关联测试通过。
- 语法检查、git diff 检查、nginx -t 及发布后内容/响应头核对通过。
- 实际只读素材来源查询返回 100 条素材，100 条均完成剧目匹配。首次
  完整查询约 23.163 秒，关联分批耗时 5.545、5.295、5.306、4.632 秒。
  冷缓存后台刷新仍需要等待，并未承诺首次刷新瞬间完成。
- 最终 Nginx、主 API、YouTube worker、TT sidecar 正常运行；TT runner
  timer、scheduler timer、runner.path 均 active。Featured 和 healthcheck
  是一次性服务，执行成功后 inactive 是预期状态。
- 公网登录页可打开。本地 topbar、TT 公共推荐接口返回 200；未登录
  请求 YouTube 素材接口返回 401。没有借用其他用户会话，故未验证
  登录后的素材 HTTP 路由；该路由底层完整只读查询已验证。
- TT 和 YouTube 已发布账本与维护前备份校验一致。业务任务、冻结配方、
  素材和 Token 均保留。停用的维护触发器已全部恢复。

## 备份与回滚

服务器备份：
`/mnt/data-disk/ai-site-performance-20261008/backup-183154`。
配置/文件原始路径和模式记录于 `manifest.json`；SQLite 在线备份
`quick_check=ok`。非敏感部署证据在
`D:\codex\ai-site-performance-20261008-evidence\deployment`。

回滚按需要选择对应项，先核对当前文件与本次部署记录的哈希。不要覆盖
之后的其他发布；不要恢复业务 SQLite、账本或 Token 来撤销代码修复。

- Nginx：恢复备份的 `/etc/nginx/default.d/drama-material-api.conf`，
  移除本次新增的 `/etc/nginx/conf.d/ai-site-cache-policy.conf` 与
  `/etc/nginx/default.d/ai-site-performance.conf`；`nginx -t` 成功后
  `systemctl reload nginx`。
- BBR：`sysctl -w net.ipv4.tcp_congestion_control=cubic`；移除本次新增的
  `/etc/sysctl.d/90-ai-site-transfer.conf`。不更改 qdisc。
- YouTube：等待准备/上传租约结束，暂时停止 worker；仅恢复备份的
  `/root/drama_material_service/features/youtube_auto_publish/drama.py`，
  检查语法，重启主 API 并恢复 worker。
- TT 健康检查：停止原本活动的调度触发器并等待准备任务结束，恢复
  manifest 中记录的 `automation_health.py`，重启 TT sidecar 并恢复
  维护前活动的触发器；不要重放队列。
- TT Featured ACL：`setfacl --restore=<backup>/tt-featured-original.acl`。

完整部署源保留于服务器数据盘
`/mnt/data-disk/ai-site-performance-20261008/release`，通过 GitHub 获取
上述精确发布提交；原始未提交开发内容未被覆盖。
