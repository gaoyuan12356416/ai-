# 开发计划

## 开发范围

V3 动态 UI 可用性、元数据性能、Dramawave 具体投放产品目录和 FB 有界范围解析。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产复现与 App ID 取证 | Codex | 浏览器、源库只读查询 | 已完成 |
| 搜索单选与首屏减负 | Codex | `assets/app.js/app.css` | 已完成 |
| 估算/能力卡/保存导航 | Codex | `assets/app.js` | 已完成 |
| 元数据并行与短缓存 | Codex | `service.py` | 已完成 |
| 具体产品同步工具 | Codex | `scripts/sync_*` | 已完成 |
| 具体产品范围解析 | Codex | `service.py/facebook.py` | 已完成 |
| 自动化与真实浏览器回归 | Codex | tests/生产浏览器 | 进行中 |
| GitHub-first 发布与回滚验证 | Codex | Git/服务器 | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile features/ad_control_v3/service.py features/ad_control_v3/channels/facebook.py
node --check features/ad_control_v3/assets/app.js
python -m unittest tests.test_ad_control_v3_ui_usability tests.test_ad_control_v3_core -v
```

## 风险与依赖

- 源库查询仅走 63350，目录写仅走 ads_ai 63353。
- 生产同步前备份 catalog 表；代码、静态资源、systemd/env 分检查点备份。
- 发布不修改现有 live pause/copy 总开关。

## 完成记录

- 2026-07-17：建立独立 clean worktree `codex/ad-control-v3-ui-fixes-20260717`，未覆盖原工作树。
