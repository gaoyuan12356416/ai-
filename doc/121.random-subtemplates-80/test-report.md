# 验收报告

2026-09-20 本地、隔离 GPU 及生产切换验收全部通过。

- `python -m unittest scripts.test_drama_catalog_versions scripts.test_drama_synthesis_cpu_catalog`：19项通过。
- 新生成/验证/staging四脚本 `py_compile` 通过，`git diff --check` 通过。
- 新42素材全部通过深检：20PNG和22透明VP9，逐帧校验2640帧；720x1280、30fps、4秒、无音轨；透明保护区域、循环边界、原设计像素误差均通过。
- 旧40条元数据及light2完整保留；四活动类各20，无重复SHA；2000种子重复性通过且覆盖全部80活动项。
- 新素材共74,123,601字节；manifest SHA256 `b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c`。
- 已人工审查42格效果图，边框/动效/角标保留中央人物和字幕空间。

精确 GitHub blob 校验已通过：manifest、原 manifest、artwork 源及 42 个媒体文件的提交内容与收据一致。一次独立视频重建 SHA 相同，合法收据复用未改变文件时间。

HK 服务器验证：

- 两个缓存均覆盖全部 80 个活动素材，逐项核对源 SHA、文件大小和 RGBA 验证收据；V2 另外保留 demux PTS/packet duration。
- 真实 Drama 服务 UID 和运行时启动预检通过，`cuda_tested=true`，80 项缓存无缺失。
- 12 段新 Drama 四层组合覆盖全部 42 个新增素材；新默认目录下分别执行原始目录和 9 月 18 日目录的冻结配方，各生成一段。
- TT 使用现网 HEVC 链路，FB 使用现网 H264 链路，各生成一段新目录样片。
- 共 16 段，每段 5 秒、720×1280、30fps、150 帧；总计 2,400 帧，视频和音频检查全部通过。该验证范围为私有合成样片，不代表线上业务发布。

没有外部测试发布。生产默认目录、公共接口、隧道和原触发器的最终状态以 `evidence/*-final-verification.json` 为准。

生产验收已完成：HK/CPU 三条链路默认 SHA 一致、四组各20；服务和隧道健康，原触发器与客户端恢复，两个旧目录仍受信。详见 deploy.md 和最终验收 JSON。
