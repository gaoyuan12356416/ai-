# X 自动模板取消 600 秒限制

2026-10-20：素材最短/最长时长可留空，保存为 null；有值时要求整数，
最短值非负、最长值为正数，两个边界同时设置时仍校验大小关系。
新建模板不再默认最长 600 秒。已有明确的 600 秒配置及冻结版本保持不变，
编辑时清空最长时长并保存，才取消该模板的筛选上限。

移除 UI、配置校验、源 SQL、两层筛选、执行器、X Sidecar 衔接、
事务入队和随机视频制作的独立 600 秒门禁。实际媒体继续使用既有账号策略：
普通账号 140 秒，实时确认的长视频账号按现有 14400 秒媒体边界；
512 MiB、媒体参数、最终会员校验、自动排重、语言及未知结果保护保持生效。
随机制作仍保留已经冻结的请求/配方，不改写已有制作凭据。
GPU 现有接口已接受 14400 秒策略，本次无需变更 GPU。

验证：

```sh
node scripts/test_x_auto_duration_ui.js
python -m unittest discover -s scripts -p 'test_x_auto*.py' -q
python -m unittest scripts.test_x_post_auto_template_bridge scripts.test_x_random_template -q
```

在 CPU 的独立 GitHub 精确提交 checkout 中执行：

```sh
python3 scripts/deploy_x_auto_duration.py
```

部署器核对线上文件指纹，复制当前 composite 后仅覆盖本次文件；先运行 Linux
回归，再暂停 X 触发器、等待运行完成、取得发布锁、备份 SQLite/令牌及代码，
同步主 API 与 Nginx 文件，切换 release 并按依赖顺序启动三个服务。
上线时核对账本行指纹、令牌指纹和静态文件，恢复原触发器状态。不产生测试 Post。
回滚只恢复代码和服务，不恢复数据库或令牌：

```sh
python3 /opt/x-post-automation/current/scripts/deploy_x_auto_duration.py --rollback /mnt/data-disk/x-post-automation/backups/<本次备份目录>
```

若已保存不限/超过 600 秒的模板，旧版本将重新执行旧限制；回滚前核对相关新任务，
必要时先停用对应模板，不改写或重放历史队列。
