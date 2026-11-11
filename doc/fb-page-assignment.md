# 批量剧目 Page 分配接口

接口根据 Dramawave 剧语言和两级画像，从当前 62 组「dramawave做排重实验」分配 Page。每次调用重新分配，并生成新的 `task_id`；关联 ID 只用于检索，不是幂等键。分配不创建广告，也不改变 Page 语言或组成员。

## 调用方式

`POST https://ai.yingliangads.com/api/integrations/v1/drama-page-assignments`

请求头为 `Content-Type: application/json` 和 `Authorization: Bearer <专用Token>`。该 Token 存储在服务器 `/etc/fb-page-assignment.env` 的 `FB_PAGE_ASSIGNMENT_API_TOKEN` 中，通过批准的凭据渠道交付；不要写入源码、URL、日志或截图。

```json
{
  "external_task_id": "调用方业务任务ID",
  "drama_ids": ["剧目content_id_1", "剧目content_id_2"]
}
```

`external_task_id` 必填，1–128 个字符；每批 1–100 个 Dramawave `content_id`，使用字符串。相同剧 ID 可以重复，分别返回并记日志。请求体最多 64 KiB。

```json
{
  "task_id": "本次调用生成的唯一ID",
  "results": [
    {"drama_id": "剧目content_id_1", "page_id": "PageID1"},
    {"drama_id": "剧目content_id_2", "page_id": "PageID2"}
  ]
}
```

响应按输入顺序返回，正常响应只含以上字段。单项无法分配时 `page_id` 为 `null`；原因保存在日志并飞书提醒。批次完成为 HTTP 200，参数错误为 400，未授权为 401，整体依赖不可用为 503。未处理的错误响应仍为 `task_id` 和空 `results` 数组。不提供 GET 任务查询接口。

## 选择规则

每请求实时读取 `kunlunads_dev.ads_facebook_page_group_ins` 的 62 组未删除成员，以及 `ads_facebook_page_ins` 的可用状态、`active_ad_count` 和 `fan_ccount`。只选择 `status=0`、`forbidden_status=0`、`is_permission=1` 的 Page。自动化读取使用既有 FIFO SQL Gate 和 63350 只读端点，不调用 Meta 写接口。

剧信息来自 Dramawave（`app_id=1479`）当前剧目数据。分类依赖线上共享 `features.x_tag_revenue` 的同版本标签与语言规则。Page 画像读取 `/mnt/data-disk/fb-page-portraits/current.json` 指向的完整本地快照，同批固定版本；请求链路不重算七天画像。无画像或最新快照读取失败时可继续选择，无画像来源故障会告警。

一级为 Page 最大收入方向与剧方向一致，并列最高的已知方向均可匹配。二级在该方向内对命中的不同题材 `share_bp` 求和，选择正分最高的 3 个候选。同分按负载排序决定入选者，之后从候选中按负载选取。

选择阶段依次为：

1. 同语言且画像匹配（`language_profile`）。
2. 同语言且无画像（`language_unprofiled`）。
3. 无语言且画像匹配（`fallback_profile`）。
4. 无语言且无画像（`fallback_unprofiled`）。
5. 同语言且放宽画像（`language_relaxed`）。
6. 无语言且放宽画像（`fallback_relaxed`）。

各阶段按 `真实活跃 ad 数 + 本批已分配次数`、粉丝数、Page ID 升序选择。只在本次请求中累加，不修改真实广告数，也不产生跨请求占用。指标缺失排在已知值之后；已知但不匹配的画像不能被当作无画像。语言不明只进入无语言池，剧不存在逐项返回未分配。

## 日志与飞书

`ads_ai.fb_drama_page_assignment_log` 每部剧一行，主键 `(task_id,item_index)` 保留重复输入。没有有效条目的整请求错误使用 `item_index=0`。正常条目从 1 开始。按外部关联 ID、剧 ID 和 Page ID 建立带时间的查询索引，所有数据库时间为 UTC。

```sql
SELECT task_id,item_index,external_task_id,drama_id,page_id,
       status,selection_stage,error_code,error_message,created_at
FROM ads_ai.fb_drama_page_assignment_log
WHERE external_task_id = '调用方业务任务ID'
ORDER BY created_at DESC,item_index
LIMIT 1000;

SELECT item_index,drama_id,page_id,diagnostics_json,
       alert_status,feishu_message_id,alert_error_code
FROM ads_ai.fb_drama_page_assignment_log
WHERE task_id = '接口返回或飞书中的task_id'
ORDER BY item_index;
```

查询使用 63350 只读端点。运行日志使用 `journalctl -u fb-page-assignment.service`，两个关联 ID 可串联全部处理记录。

先把冻结结果持久化到数据盘 SQLite 待补写队列，再尝试使用专用 writer 配置写 63353 的这一个日志表。MySQL 失败时继续返回结果、提醒郜远并后台退避补写；补写使用原主键和原内容，不重新分配。写入未知不能根据只读副本尚未查到就重建任务。若本地持久化也失败，返回 503 并告警。

放宽画像、无可用 Page、已鉴权请求错误、依赖异常或日志待补写时，异步向郜远发送汇总提醒，含两个 ID、剧 ID、选中 Page、原因及北京时间。正常无画像选择不等于异常。未授权请求不发飞书。发送失败不改变分配结果，未知投递保留原 UUID 和回执状态。

## 部署与回滚

部署前提交并推送精确版本到 `gaoyuan12356416/ai-`，在 CPU 主机取得该提交的干净 checkout。安装器只复制新模块和独立服务；共享主 API、发布服务和定时器不被替换或重启。

```bash
python3 CHECKOUT/ops/fb-page-assignment/deploy.py \
  --source CHECKOUT --commit FULL_GITHUB_COMMIT \
  --deployment /mnt/data-disk/fb-page-assignment/deployments/UNIQUE_DEPLOYMENT \
  --create-table
```

`--create-table` 仅在部署时执行经审核的单表 DDL，验证 `DATABASE()='ads_ai'`、`@@read_only=0` 和确切表名。应用运行时不做 MySQL DDL。已有表的后续版本部署可省略该参数。首次建表及本功能的写入由本次功能实施授权覆盖。

安装器核对数据盘 UUID、精确提交和文件 hash，保留配置与服务备份。代码放在 `/mnt/data-disk/fb-page-assignment/releases/<commit>`，`/opt/fb-page-assignment/current` 指向当前版本。配置文件权限为 0600，首次生成 Token，升级保留原 Token。监听 `127.0.0.1:8943`，Nginx 仅代理精确 POST 路径。`/healthz` 只通过本机访问，不暴露凭据。

安装后核对服务健康、未授权响应、正常分配、数据库记录，以及一条带两个 ID 的验收飞书提醒。同步失败、通知失败、重试和重复输入通过隔离测试覆盖，不用关闭生产数据库进行故障注入。

```bash
python3 CHECKOUT/ops/fb-page-assignment/deploy.py \
  --deployment /mnt/data-disk/fb-page-assignment/deployments/UNIQUE_DEPLOYMENT \
  --rollback
```

回滚恢复之前的接口路由、代码指针、配置和该服务的启用状态；首次安装的回滚撤下新接口。数据库表、日志、待补写队列、通知回执和已交付的 Token 保留。安装失败会自动执行同样的代码回滚；已经存在更新版本时拒绝用旧部署覆盖。

现有画像定时刷新超时问题不包含在此次部署内；始终记录实际使用的完整快照及时间。
