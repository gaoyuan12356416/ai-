# 开发计划

## 范围

1. 修改正式素材池候选时长边界。
2. 修改 Premium GPU 修复边界并升级 profile。
3. 保持自动模板 600 秒隔离。
4. 更新素材池文案和个人技能规则。
5. 执行定向与全量 X 回归。

## 验证命令

```powershell
python -m py_compile features/x_posts/selector.py features/x_posts/media_repair.py scripts/x_post_daily_runner.py
python -m unittest scripts.test_x_post_material_pool_selector scripts.test_x_post_media_repair scripts.test_x_post_daily scripts.test_x_post_multi_schedule_ui
python -m unittest scripts.test_x_post_auto_template_bridge scripts.test_x_auto_post_validation scripts.test_x_auto_publish_ui
python -m unittest discover -s scripts -p "test_x*.py"
git diff --check
```

## 风险

- 生产部署必须同时更新 CPU 和 GPU；v4 profile 不匹配时应失败关闭。
- 会员资格在计划和最终发布之间可能变化，最终账号 Token 检查仍为权威门禁。
