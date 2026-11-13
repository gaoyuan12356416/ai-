# Moboreels 发布资格校验

仅使用 `{{m_url}}` 或已冻结 Moboreels 身份的社媒任务参与校验。服务端从素材取得上游 `story_id`，通过受限内部接口读取本机构剧库的当前记录；只有剧 ID 完全匹配且 `is_show=1` 才继续。未匹配、下架、状态读取失败均返回中文拦截原因。其他产品沿用原逻辑。

前端沿用现有预检及提交接口，并增加规则提示。FB、X、TikTok、YouTube 的已保存链接、准备结果和实际平台写入入口再次检查，防止预检之后下架仍继续发布。已提交的平台请求仍按原回执核对，不重置 unknown 或重发。历史链接不删除。已经在 YouTube 平台预约成功的视频不自动撤回；本次检查发生在新上传、公开或预约操作之前。

`runtime/` 以各服务此次实际线上文件为基线，只加入校验，保留并行上线的 FB 文案、指标和剧目受众改动。`manifest.json` 固定原始路径、逐文件前后摘要。常规读取沿用现有内部接口；不更改数据库表结构或发布账本。

## 验证

- `python -m unittest discover -s tests -p test_moboreels_publish_eligibility.py`：当前上架、下架、未知状态、剧 ID 不匹配、预检后下架、已有链接复用、其他产品隔离、原生发布入口和请求回执保护。
- `FB_TEST_OVERLAY=1 FB_ELIGIBILITY_OVERLAY=1 python -m unittest discover -s tests -p test_moboreels_fb.py`：实际生产功能覆盖，17 项。
- 既有社媒适配器、YouTube、TikTok 发布单测及两组前端宏测试。
- 所有部署 Python 文件编译；逐文件摘要核对；上线后以真实上架/下架剧目只读校验，不创建社媒帖子。

## 上线与回滚

先推送 GitHub，在服务器 fetch 后建立精确 SHA 的 release。所有下列命令使用 `/usr/bin/python3 RELEASE/ops/moboreels-publish-eligibility-20261113/deploy.py SHA` 为前缀。

1. `stage`：检查实际目录和摘要，备份当前文件，并克隆独立的渠道服务候选版本到数据盘。保存返回的部署目录为 `DEPLOYMENT`。
2. 分渠道执行 `pause --deployment DEPLOYMENT --group GROUP`（`x`、`fb`、`tt`，最后 `youtube`）。仅暂停该渠道新任务，保留其他渠道和现有在途任务。
3. 用 `busy --deployment DEPLOYMENT --group GROUP` 确认该渠道执行租约自然清空，再执行 `activate`，随后执行 `resume`（同样参数）。TT 与 TT 自动发布共用一次暂停。恢复原有调度守护进程、timer/path 并核对健康。若配置版本变化，先核对中间事件，再传 `--reviewed-version`；禁止盲目覆盖并行配置。
4. 已恢复服务的回滚：执行 `rollback-pause`，等待 `busy` 为零，再执行 `rollback`，均带相同部署目录、渠道参数。尚未恢复且部分激活失败时，直接在原暂停内执行 `rollback`。工具仅恢复本次文件和指针，遇到无关新部署或文件改变即停止，保留剧库、推广记录、所有发布账本和请求回执。

完整代码树摘要、文件备份、暂停/恢复回执和调度快照存放在 `DEPLOYMENT`，仅服务器 root 可读。回滚不重跑历史任务。
