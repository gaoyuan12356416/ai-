# DramaWave TT 推广新版

频道管理与自动发布分别使用 `tt_promotion_channels` 和 `tt_promotion_auto`。管理员可用现有权限管理页面分别授权。频道运营设置和任务位于 CPU 的 `/mnt/data-disk/tt-promotion/promotion.sqlite3`。新模板创建为草稿。首次上线的调度器只处理已经启用的新版模板以及运营主动提交的新版运行。

## 验证与切换

使用已推送的 GitHub 提交，在 CPU 数据盘的独立检出中运行 `python3 ops/tt-promotion/deploy.py stage COMMIT`、`init COMMIT`。先以实际 TT 服务账号及现有 TT 环境运行候选目录下的 `verify_runtime.py`。该脚本在临时库中建草稿并调用预演，读取真实账号、画像和素材。发布日报及效果仓分别从新版冻结计划及确认回执读入，统一归入 TT 自动模板。

运行 `python3 ops/tt-promotion/deploy.py activate COMMIT` 激活。检查两个新入口、登录与各自的权限、独立定时器、原 TT 发布触发器和公共报表。主动执行正式任务会产生真实 TikTok 帖子，应由运营选择频道、素材和时段。试运行前在频道详情检查旧排期和在途任务；并行期间旧发布器不会读取新版的预约。

## 回滚

在原提交的检出目录使用 `python3 ops/tt-promotion/deploy.py rollback COMMIT`。部署清单与原文件位于 `/mnt/data-disk/tt-promotion-releases/COMMIT`。脚本逐项比对部署指纹，遇到后续改动时停止。已有真实任务时保留读取这些事实所需的报表来源，外部提交待核对时先完成核对。频道配置、任务、作品回执、已分配的归因码与收入历史保存在数据盘。

隐藏旧菜单和停用旧模板分别由运营操作。旧账本保留给统一报表和历史核对使用。
