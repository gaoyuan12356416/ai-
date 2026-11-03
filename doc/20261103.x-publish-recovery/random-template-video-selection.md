# 随机视频模板的规划输入过滤

q5620 使用原素材 7676040 的 PNG URL，冻结批次启用了随机视频模板。委托规划此前接受图片，节点预检因 `x_random_requires_video` 拒绝，未进入随机模板准备或平台写入。该条任务的历史结算独立处理，本改动不恢复或修改既有队列。

`execute_schedule_tick` 读取原冻结 run 的 `random_template_enabled`，仅当该开关开启时，为正常 material loader 传入 `allow_images=False`。选材器沿既有 `PoolCandidateRejection('material_not_video')` 路径逐项记录图片拒绝，继续原池快照、页序和账号分配。元数据查询重连仍保留同一策略。选材器默认 `allow_images=True`，其它调用行为不变；已有队列在选材前直接交接。

运行文件仅两项：`scripts/x_post_schedule_runner.py`、`features/x_posts/selector.py`。GitHub 提交后可由维护者增量同步到下一轮 runner 所用的同一 release；当前扫描进程不需重启，不修改 node/coordinator，也不探测或下载媒体。

验证：`python -m unittest scripts.test_x_schedule_random_video_selection scripts.test_x_post_material_pool_selector scripts.test_x_post_schedule_runner scripts.test_pipeline_x_planning`，123 项通过。新增集成覆盖真实元数据选择、图片先于视频的跨页选择、两种语言账号继续、关闭模板兼容、只读查询重连、冻结设置优先和已有队列不重选。
