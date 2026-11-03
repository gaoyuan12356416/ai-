# 封面 COS 读取重试与入库阶段报错

三比例图片已经生成时，入库前读取 COS 的 HTTPS 握手超时曾被统一显示为“图片生成进程超时”。
现在回调异常带明确的素材入库阶段标记，通知区分图片生成失败和图片已生成但素材入库失败。
历史错误只在 96% 且明确显示“正在回传素材接口”、三图 URL 均存在时按入库阶段解释；不能仅凭有旧图片判定生成成功。

公共图片 GET 的连接超时为 10 秒、读取超时为 40 秒，最多 3 次，等待 1/3 秒。
只重试连接/读取超时、断连、分块流中断和 HTTP 408/429/500/502/503/504；
每次丢弃未完整下载的数据。鉴权、证书、图片格式、SHA、比例校验失败不做网络重试。
POST、逐图签名、回执及正式素材对账代码保持不变：成功项直接复用回执，结果未知项只对账，不能重发。

验证：`python scripts/test_cover_material_source.py`、`python scripts/test_cover_ingestion_retry.py`，
以及 `python scripts/test_cover_ingestion_ops.py`。均为离线测试，不提交真实素材。

## 部署

先将分支推送到 GitHub，服务器 fetch 精确 SHA，并 git archive 到
`/mnt/data-disk/deploy/cover-ingestion-retry/releases/<SHA>`。
运行该 release 的 `ops/cover-ingestion-retry/deploy.py prepare --repo <repo> --commit <SHA> --backup <backup>`。
工具核对 Git blob、当前 app/模块及三个目标函数哈希，备份并组合候选 app，证明其他 AST 不变。
仅在截图队列排空后短暂停止 `drama-material-api.service`，执行同一工具 `apply --backup <backup>`，
再启动主 API 并核对健康。图片生成服务、YouTube、日报及发布服务不重启。

## 仅补完指定任务的入库

工具 `ops/cover-ingestion-retry/recover.py` 接受 `freeze|recover --job-id <id> --state <evidence-dir>`。
必须明确选择一个失败且已有三图的任务；冻结完整原任务、图片 SHA、逐图账本和在线 SQLite 备份。
recover 直接调用共享入库模块，不进入生成队列。已有 confirmed 记录必须逐字不变；
prepared/新图片最多发起一次，unknown/accepted 继续对账。全部三图具有有效回执和正式库确认后，
对比原任务行再仅更新状态、进度、结束时间，清除错误。图片路径、URL、人员和生成计数不变。
中途失败保留账本，从同一证据目录继续；不能清除账本强制重发。

回退仅恢复本次备份的两个代码文件：停止主 API，运行同一 deploy.py
`rollback --backup <backup>`，启动主 API。禁止恢复 SQLite 或删除已入库素材/回执。
