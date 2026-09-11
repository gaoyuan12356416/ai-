# AI 封面原生尺寸与 16:9 裁剪

生图工具可能返回接近 16:9 的原生尺寸。以前提示词要求精确 1536×864 或 1920×1080，生成器会因 1672×941 不匹配而不保存文件，导致 `cover_generation_output_missing`。

现改为保存原生输出，再由后台严格解码并做少量居中裁剪。适用于本流程新完成的 AI 生图，包括首次、重新生成和审核打回；历史封面、已审核资产、原剧参考图、手动上传和其他生图模块不变。

- 提示词要求接近 16:9，人物、标题和主体距四边至少 3%，不再强制两个精确尺寸。模型只保存原始图片，不负责缩放、裁剪或改头。
- 先检查完整文件格式、PNG CRC、真实扫描线、尺寸和原有比例容差 `abs(w/h-16/9)<=0.03`。损坏图片继续拒绝。
- 计算 `k=min(w//16,h//9)`，居中裁成 `16k×9k`。不拉伸、不补边、不重画、不放大。
- 每个方向移除的总长度最多为该方向的 2%，移除的总面积最多 3%，裁后至少 320×180；超限报 `generated_cover_crop_excessive`，要求重新生成。显著偏离横版的图仍报比例错误。
- 1672×941 → 1664×936：左 4、右 4、上 2、下 3 像素，保留约 99% 面积。奇数余量分配给右/下。
- 原生 `cover.png` 保持字节不变。需要裁剪时新建私有 `cover-normalized.png`；`generation-output.json` 记录前后尺寸、裁剪框、四边像素数、SHA 和策略版本。返回裁后图片供原流程转 JPEG、展示和人工审核。
- 已为精确 16:9 的输出保持原字节。现有 `decode_cover` 的只读行为不变，读取历史图不会触发裁剪。

少量裁边和提示词安全边距能限制布局影响，但不能语义证明任何边缘标题或人脸都安全，因此继续通过现有人工审核后发布。

## 验证

`python -m unittest discover -s scripts -p test_youtube_cover_crop.py`：验证上述实例的每个裁后像素等于原区域像素、原图不变、精确比例不处理、宽/高偏差、裁剪幅度保护、损坏文件拒绝、生成适配器到 JPEG 审核资产端到端。

兼容回归：`test_youtube_failure_images_runtime.py`、`test_youtube_reference_runtime.py`、`test_youtube_auto_service.py`。不执行真实生图、消息、上传或首评测试。

## 部署与回滚

`scripts/deploy_youtube_cover_crop.py --commit <完整SHA> --check` 后去掉 `--check` 部署；必须从 CPU 的 GitHub 镜像 fetch 对应提交、归档到数据盘 release 并写入 `.github-verified-commit`。脚本验证两个线上模块基线 SHA，先备份再按依赖顺序原子替换，仅向自动发布 worker 的 MainPID 发 SIGTERM。它在当前迭代完成后自然退出并由 Restart=always 载入新代码。无 API、旧 worker、统一 writer、HK 服务重启。

确认新 worker PID、active 状态、线上模块 SHA、真实样本解码和账本/短链状态。不会恢复数据库或替换任何现有资产。回滚使用同 release 的 `python3 scripts/deploy_youtube_cover_crop.py --rollback <backup>`；先恢复旧 runtime 再恢复 images，检测后续修改与备份摘要，保留数据库、通知与图片，再让 worker 平滑加载旧代码。
