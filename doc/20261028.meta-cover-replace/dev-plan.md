# 开发计划与完成记录

| 工作项 | 模块 | 状态 |
| --- | --- | --- |
| 小时扫描、阈值、封面来源、CLI | cover_source.py、cover.py | 已实现，主实现集成检查 |
| 参数转换、Graph、统一调速 | cover_payload.py、cover_graph.py、throttle.py | 已实现，离线验证 |
| 持久队列、单次删除、共享互斥 | cover_store.py、features/fb_ad_asset_delete/store.py | 已实现，离线验证 |
| 32 worker、引用、未知核实 | cover_worker.py | 已实现，持续集成 |
| GitHub-first、备份、systemd、回滚 | deploy/install_cover.py、meta-cover-* | 发布待执行 |
| 需求/评审/测试/API/部署文档 | 本目录、模块 SKILL.md | 已编写 |

## 本地检查

从仓库根目录执行：

```bash
python -m compileall -q server-monitors/meta-violation-material-cleanup/meta_violation_cleanup features/fb_ad_asset_delete/store.py server-monitors/meta-violation-material-cleanup/cover.py
python -m unittest discover -s server-monitors/meta-violation-material-cleanup/tests -p 'test_cover*.py' -v
python -m unittest discover -s tests -p 'test_fb_ad_asset_delete_*store.py' -v
git diff --check
```

结果见 test-report.md。Graph 测试使用模拟传输，不产生真实写入。生产依赖已有 Meta 权限、Meta 可拉取的封面 URL、只读 SQL Gate、Feishu 配置。凭证不进入仓库或日志。最终完整测试数量和生产回执由发布负责人补充。
