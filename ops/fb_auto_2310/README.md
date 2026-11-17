# FB 自动模板统一 2310

新建旧版自动模板任务使用 `https://www.dramawavew2a.com/ads/101/2310/view`。
历史帖子、短链地址、原始冻结 URL、campaign ID、Page ID、素材 ID 和 Graph 回执均保留。
Nginx 只在 `/s2l/fb/` 的 HTML 响应中，把旧落地页完整前缀替换成 2310，覆盖 meta、canonical、JavaScript 和备用锚点；查询参数字节不变。磁盘原文件不改，任务不会被重发。

报表沿用 `2049` 作为旧版发布来源的内部身份，仍按原模板创建人归属；同一记录的效果允许来自 2049 或 2310。索引保留全目录归属冲突检查，收入匹配仍核对冻结 Page/素材 ID。发布数、生成链接数只计一次，来源表按实际收入站点显示。旧自动模板反馈收入查询也兼容两站点。

部署前运行相关测试：165 项通过，5 项平台相关跳过。`patches.py` 对每份实际运行文件作局部修改，保留主 API、日报、数仓和 SocialKit 对比服务之间已存在的实现差异；部署文件清单不包含本分支为重现测试而补齐的历史报告依赖。

2026-11-17 已上线：全量核对 20,291 条已发布任务关联的历史短链，四个跳转目标均为 2310，原查询参数、冻结链接和 Graph 回执保留；12 条分段公网抽样与 3 个落地页 HTTP 均为 200。浏览器实际打开历史短链确认最终 2310、剧目匹配、八个归因字段完整与 CTA 函数存在。主 API、FB owner、FB 调度及原暂停的 YouTube worker 状态核对通过；新任务真实生成函数预演使用 2310，切换后普通生产任务继续有 published 回执。验收时尚无新建的旧模板生产任务，未创建测试帖。

浏览器全程核验发现旧短链 CSP 中浏览器不识别的 `navigate-to` 指令，已从本次短链配置删除；其它有效 CSP 限制保留。公开跳转和原始参数不受这项清理影响。

部署目录 `/mnt/data-disk/fb-auto-2310-20261117/deployment`。在已推送的干净 GitHub checkout 中执行：

```sh
python3 ops/fb_auto_2310/deploy.py prepare /mnt/data-disk/fb-auto-2310-20261117/deployment
python3 ops/fb_auto_2310/deploy.py activate /mnt/data-disk/fb-auto-2310-20261117/deployment
```

主 API 使用已安装的 ExecStop 任务排空屏障，只窄切 FB owner；调度使用发布健康检查的 snapshot/restore，保留原来暂停的入口。Nginx 校验后只 reload。日报等一次性任务在下次运行读新源码，不为文件变化触发报表发送或重复执行。

验收：对每个报告 runtime 运行 `validate.py --root ROOT --mode report`，对 FB runtime 用 `--mode fb`；`validate.py --verify-links DEPLOYMENT` 全量核对历史已发布短链的四个跳转目标与参数，并抽查公网短链、2310 落地页和后续实际新任务。

安全回滚（只恢复路由，新产生的 2310 任务和报表兼容继续保留）：

```sh
python3 /mnt/data-disk/fb-auto-2310-20261117/source/ops/fb_auto_2310/deploy.py rollback /mnt/data-disk/fb-auto-2310-20261117/deployment
```

备份包括原 FB release、逐文件前后哈希和副本、Nginx 原配置、FB SQLite 在线备份、调度状态。回滚绝不把旧 SQLite 覆盖当前数据库，不清任务、回执、未知结果或租约。已有后续代码漂移时先核对，不覆盖其他变更。
