# X 视频体积旧限制移除

下载、预检、冻结队列、最终上传以及 GPU 输入/输出取消旧的 512 MiB
应用限制。普通账号按 X Post 视频 8 GiB、经令牌确认的会员按 16 GiB
处理；最终上传重新使用当前账号的资格检查。媒体传输和 GPU 配置以
16 GiB 为接口最高边界，图片/GIF限制、时长策略和历史任务不变。

官方来源（2026-10-21核实）：
- https://docs.x.com/x-api/media/quickstart/best-practices
- https://docs.x.com/x-api/media/initialize-media-upload (`total_bytes <= 17179869184`)
- https://docs.x.com/x-api/media/append-media-upload (`segment_index <= 9999`)

取消上传器1000分片的旧限制，使用10000分片边界。媒体HTTP错误仍按2 MiB
读取，避免将大文件额度用作错误正文内存额度。GPU正常码率仍为5 Mbps，
只有真正接近成片预算才降低码率；显式旧预算仍兼容。

## 验证和上线

`python -m unittest scripts.test_x_media_size scripts.test_x_posts scripts.test_x_post_daily scripts.test_x_post_schedule_runner scripts.test_x_post_media_repair scripts.test_x_auto_post_service scripts.test_x_random_template scripts.test_x_post_auto_template_bridge -q`

精确GitHub提交在两台主机独立检出后：

1. 各自运行 `scripts/deploy_x_media_size.py --host cpu|gpu --stage`。
   校验部署前文件SHA256，仅将相关文件叠加在现有组合版本上，并执行回归。
2. CPU运行 `python3 scripts/deploy_x_media_size.py --host cpu --deploy <CPU备份目录>`。
   暂停原活动触发器、等待发布任务排空、持有发布锁，备份数据库并停止入口服务。
   状态到 `waiting_for_gpu` 后执行下一步；最多等待180秒，失败自动恢复。
3. GPU运行其运行时Python执行 `scripts/deploy_x_media_size.py --host gpu --deploy <GPU备份目录>`。
   先确认没有活动请求，备份配置后切换，恢复媒体修复服务及反向隧道。
4. CPU读回GPU健康接口的16 GiB额度后自动更新四个专用env键，切换代码并
   按依赖顺序启动服务，比较全部发布账本与令牌指纹，恢复原触发器状态。
5. 不创建测试Post，不重放历史失败任务。真实素材验证只下载/探测/按需GPU
   修复，验证文件大小、时长和编码，并将私有回执留在操作目录。

## 回滚

使用同一GitHub检出目录执行：

`python3 scripts/deploy_x_media_size.py --host cpu --rollback <CPU备份目录>`

GPU按同样命令使用 `--host gpu` 和GPU运行时Python。CPU先回滚，GPU应在
所有CPU生产入口排空后回滚；保留增量兼容的GPU版本也可。回滚仅恢复代码和
配置，保留数据库、Token及发布历史。旧版本会重新拒绝超过512 MiB的待执行文件。

部署清单、回归结果、精确提交、备份路径与实时素材验证回执保存在私有操作输出中。
