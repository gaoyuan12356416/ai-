# 随机模板新增20个子模板

用户于2026-09-18批准参考页中的全部20个方案上线，四类各5个，覆盖所有采用随机模板的制作入口。

保留原18个有效资产和2个历史light资产；新增后 border=8、opacity_video=10、corners=8、tint=12，四层基础组合7680。PNG为720x1280 RGBA；动态层为720x1280、30fps、4秒透明VP9 WebM。不包含预览中的人物、文字和原视频。

新建TT/FB准备任务和Drama创建任务使用扩展目录。Drama创建时冻结的旧配方、TT已有ready成片按原目录继续读取；FB现有ready manifest直接复用。不改历史配方，不重新制作已完成产物，不触发真实发布。CPU仅保存目录元数据，GPU保存素材并建立两套RGBA缓存。

可信历史目录映射由部署环境RANDOM_OVERLAY_ASSET_CATALOGS提供，SHA映射到绝对目录；请求只携带SHA。未知SHA拒绝，原manifest与每项资产SHA校验保留。默认目录可回退，但保留新旧目录映射与兼容代码，以便处理已经冻结的新配方。

参考页：https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/codex-artifacts/html/20260918/random-subtemplates-reference-172953-3b282f/index.html
