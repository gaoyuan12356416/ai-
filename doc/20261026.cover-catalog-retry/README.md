# 封面素材库断线恢复

目标任务 `e2a511bd8a664b6b8279e7c3a42f42d8`，moboreels 3360，剧目 132516419。
2026-10-26 11:09:51 北京时间在素材入库前的只读查询返回 MySQL 2013。
三张图均已生成；1:1/1.91:1 已各提交一次并确认正式 ID 7835627/7835640；
4:5 签名账本仍为 prepared、attempts=0。旧代码把所有 catalog 读取错误直接抛出，
任务自动重试分类不识别 catalog_read_failed_2013，因此停在 96% 并告警。

修复仅在 Catalog.query 内重试连接错误 2002/2003/2006/2013/2055，最多重试两次，
等待 1/3 秒；每次重建经过 FIFO Gate 的只读连接并重复验证 @@read_only。
权限、SQL、查询超时、Gate 失败、无法确认只读或非法响应均不重试，不把失败作为空结果。
POST 和逐图账本语义不变；已提交但结果未知的图仍只允许对账。

66 项离线契约测试通过，包括部分响应丢弃、有限重试、日志脱敏、提交前后断线不重复 POST。
命令：`python scripts/test_cover_material_source.py`。

部署只替换 CPU 主 API 的 features/cover_material_source.py，生产组合 app.py 保持字节不变。
先 push/fetch 精确 GitHub SHA，解包到 `/mnt/data-disk/deploy/cover-catalog-retry/releases/<SHA>`；
验证旧模块 SHA256 `ea15d3e3776e3c5f966a805d0901ef79d148ab6bf768827649bb5cf09c557bb1`，
备份模块和服务 PID，原子安装后仅重启 drama-material-api.service；图片生成 sidecar 不重启。
原任务/队列/图片/出站账本保留，API 既有启动恢复从已保存产物继续。

本目标已有完整图片，使用范围固定的 callback-only 修复工具避免重新进入图片生成队列：
`python3 ops/cover-catalog-retry-20261026/recover.py freeze` 保存在线 SQLite 备份、完整目标行、
逐图账本、三张公网图解码/尺寸/SHA。`recover` 仅调用现有上传回调；前两张复用回执，
第三张先查签名再提交。全部正式字段确认、图片 SHA 不变且目标行未发生并发变化后，
在 SQLite 写锁内仅更新本目标为 done/100%，保留历史失败快照、已生成图片和 token 计数。
任何状态漂移都停止，未知回执不得删除后重试。

取证目录 `/mnt/data-disk/codex/tasks/cover-catalog-retry-20261026`，备份仅用于取证。
代码回退只原子恢复本次备份模块并窄重启 API，禁止恢复 SQLite、删除回执或撤回已经入库的素材。
最终版本、回滚命令、PIDs 和真实验收另见 production.md。
