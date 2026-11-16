# YouTube 社媒素材原剧产品映射

素材 8365481（AI小语种-en-31181-19:25-31181）存放于“Drama-社媒专用素材”3214，其英文原素材 8364331 和原剧资料属于 Dramawave 1479。原查询直接把素材库 ID 当成剧集产品 ID，导致剧名、简介和原剧封面同时 missing。

素材 SELECT 仅将已知素材库 3214 映射到 1479。其他产品、素材范围、content_id、语言、上传人、短链和历史发布记录保持现有规则。剧集解析器继续按产品、精确 content_id、语言匹配，保留同产品冲突检查。

该 SELECT 是每次请求读取的配置，配置文本也是缓存键。原子替换 SQL 即可使新读取使用新映射，不重启主 API、发布 worker 或协调器，不暂停调度，不改写已选草稿及历史任务。已打开的发布表单需重新选择素材。

先推送本版本，在 CPU 从 GitHub fetch 精确 SHA，建立只读源码 checkout。用运行目录的 `MaterialSource` 和 `DramaMetadataResolver` 对候选 SQL 只读核验 8365481、8364331 及其他产品样本，再执行：

```bash
python3 RELEASE/ops/youtube-social-material-app-20261116/deploy.py COMMIT
```

安装器只备份和替换 `/etc/youtube-auto-publish/material-source.sql`，输出备份目录并记录 SHA、GitHub commit 及相关服务 PID。安装前拒绝覆盖本次基线之外的并行修改。素材范围未变，上传人 SELECT 无需修改。

上线后通过真实登录会话的素材 GET 核对 8365481 的 app_id=1479、drama_status=matched、非空 macro_name/macro_desc、drama_cover_status=available，并实际读取原剧封面。仅查询，不创建任务、生图或发布。检查相关服务 PID 未因本次部署改变。

精确回滚命令：

```bash
python3 RELEASE/ops/youtube-social-material-app-20261116/deploy.py COMMIT --rollback BACKUP_DIRECTORY
```

回滚仅恢复本次 SQL，保留当前数据库、封面资产、短链、凭据及全部已发生的发布事实；若 SQL 已有后续修改则先核对差异。
