# 独立素材剧集关联修复

用户报告：素材 `data_source_id` 有值，选择后 `{desc}` 与 `{url}` 仍被判定缺少剧集关联。

## 原因与修改

- 当前素材 SQL 将 `data_source_id` 投影到 `content_id`，但 `macro_desc` 固定空串、`macro_name` 使用视频文件名，没有查询 `ads_drama_resource`。
- 原 URL 校验只支持原剧集合成任务。`task_id=-2` 的独立素材没有原合成任务，却有有效剧集 ID。
- 增加独立的批量剧集信息解析器，按精确 content_id 和去首尾空白、忽略大小写的语言匹配。查询强制使用 content_id 索引、SQL DISTINCT 去除按集重复的相同资料，最多 1000 组结果，沿用只读副本/超时/限流。十六进制参数显式使用 utf8mb4_unicode_ci，避免生产库列与连接排序规则不同造成 1267 错误。
- 无匹配或资料冲突时保留素材并显示具体提示；不会猜测剧名、简介或跨语言关联。提交重新读取资料并冻结快照。
- 独立素材使用独立 `link_job_id`，取本次发布准备任务 ID。该 ID 是 SHA-256 对版本命名空间、租户、用户、operation_id 的 JSON 编码取前 32 位十六进制，确保重试和并发身份稳定。原 source_job_id 留空，已有合成链接不变。
- 原短链存储仅新增允许 `custom_source` 视频类型；长链地址、参数顺序、编码、不可变规则、短链路径均复用已有实现。
- 前端使用后端 link_ready 校验独立素材，预览真实剧名与简介，并解释独立素材无需原合成任务。

## 验证证据

2026-09-10 在 CPU 服务器内存中加载候选解析代码，使用只读副本和现有正式 SQL 验证，未写生产文件、数据库、短链、飞书或 YouTube：

| 素材 ID | content_id | 语言 | 简介字数 | 结果 |
| --- | --- | --- | --- | --- |
| 6617751 | wpb8VBAlOz | ko | 146 | matched / link_ready |
| 6617770 | 7Bw77v5k3t | zh-TW | 109 | matched / link_ready |
| 6617776 | jAWo9yLSpu | ja | 173 | matched / link_ready |

第二条剧名：我孵化了我的龍丈夫，王子們後悔了。列表及资料合计两次 SQL，实测 4.70 秒；不在工作台初始化时查询。既有 20 秒列表缓存保留。五项素材筛选条件、当前 SQL 配置和数据库数据均不变。

自动验证：专项 14 项、准备服务 43 项、发布引擎 43 项、HTTP 17 项、原剧集合成回归 86 项，合计 203 项通过（包含加载优化提交后的服务/HTTP 回归）。浏览器 11 项通过，验证 1440×1000 / 1280×900、三个宏、正常提交、真实缺失时拦截及无 JS 异常；全部接口使用 localhost mock，未创建真实发布。

截图位于 `output/playwright/youtube-drama-association-{1440,1280}.png`。

## 集成与发布注意

本修复使用独立 worktree，已基于主任务加载优化提交 `e958789` 集成并重新验证服务与浏览器。HTML script 版本为 `20260910-loading-drama-v1`。新增 `features/youtube_auto_publish/drama.py` 必须随包发布。未修改主任务工作目录；用户再次要求完成修复后，已按下面记录发布生产。

既有 `deploy_youtube_auto_publish.py` 为首次部署脚本，本次不可重新跑首次部署，也不能覆盖已启用的 SQL。发布只更新本修复 Python/页面文件，事前校验当前线上文件与已确认基线一致、备份文件与 SQLite，确认没有正在上传/评论的任务后重启受影响服务。回滚只还原代码，不删除短链或准备任务账本，不回放真实发布。

共享个人 skill 的 YouTube 参考文档已更新当前关联规则和上线记录。

## 生产发布完成（2026-09-10 17:38）

- GitHub/服务器归档提交：`1c3f4b172646e1758a817ceacbe4385f5997da71`，分支 `codex/youtube-drama-association-20260910`。服务器独立镜像 fetch 后校验 FETCH_HEAD 与提交一致，再从该提交归档；未使用本地散文件覆盖生产。
- 发布器：`scripts/deploy_youtube_drama_association.py`。上线前逐文件匹配加载优化基线、SQL 摘要、数据盘 UUID、空闲账本、feature guard。生产 Python 3.9 重跑专项14/服务43/HTTP17/引擎43，117 项通过。
- 更新 8 个源文件及 2 份 Nginx 页面文件。短暂停止/启动主 API、原 YouTube worker、新封面审核 worker；统一 writer 未重启。发布后四服务 active，NRestarts=0。
- 备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/association-20260910-173818-1c3f4b172646`，包含文件清单、SHA、SQLite 在线备份和结果。已有五条发布账本、五条短链及零准备任务均保持不变。
- 使用当前操作者既有有效会话对运行中的 HTTP 服务执行只读 GET，未创建会话或改变权限。`bootstrap?include_channels=0` 返回200，30ms；搜索 `6617770` 返回200，matched/link_ready=true、繁体剧名及109字简介；全部素材返回3条 matched/link_ready=true（韩语146、繁体109、日语173字）。冷查询分别约4.54/4.48秒。
- 公网 HTML/JS 均200，内容字节与运行源文件一致，版本 `20260910-loading-drama-v1`；匿名素材API继续401。原SQL SHA `c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01` 不变。
- 未执行真实生图、飞书提醒、视频或评论测试；原有加载优化保留。浏览器连接本轮不可用，线上验证采用真实鉴权 HTTP 返回与公网静态字节核验；此前本地真实浏览器11项交互已通过。

回滚仅还原该补丁文件并保留账本、SQL与短链：

```sh
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/1c3f4b172646e1758a817ceacbe4385f5997da71/scripts/deploy_youtube_drama_association.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/association-20260910-173818-1c3f4b172646
```

回滚器会先确认当前文件仍属于本补丁且没有正在发布/生成的任务，发现后续代码变更会拒绝覆盖。
