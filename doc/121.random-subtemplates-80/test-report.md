# 验收报告

2026-09-20 本地验收通过，线上验收待执行。

- `python -m unittest scripts.test_drama_catalog_versions scripts.test_drama_synthesis_cpu_catalog`：19项通过。
- 新生成/验证/staging四脚本 `py_compile` 通过，`git diff --check` 通过。
- 新42素材全部通过深检：20PNG和22透明VP9，逐帧校验2640帧；720x1280、30fps、4秒、无音轨；透明保护区域、循环边界、原设计像素误差均通过。
- 旧40条元数据及light2完整保留；四活动类各20，无重复SHA；2000种子重复性通过且覆盖全部80活动项。
- 新素材共74,123,601字节；manifest SHA256 `b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c`。
- 已人工审查42格效果图，边框/动效/角标保留中央人物和字幕空间。

后续门槛：精确GitHub blob校验；HK双缓存离线验证、实际服务UID和GPU样片；三版本配方兼容；GPU/CPU切默认目录，健康及触发器恢复。没有外部测试发布。
