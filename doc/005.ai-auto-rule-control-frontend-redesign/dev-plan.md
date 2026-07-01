# 开发计划

## 开发范围
本阶段仅做可评审静态原型和需求文档，不改生产代码。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 梳理现有前端问题 | Codex | `static/ad-control-pages.js` | 完成 |
| 创建需求目录 | Codex | `doc/005...` | 完成 |
| 设计方案向导原型 | Codex | `prototype/ad-control-redesign-preview.html` | 完成 |
| 补充评审文档 | Codex | `requirements.md`、`sa-review.md` | 完成 |
| 用户评审 | 用户 | 原型页面 | 待确认 |
| 正式实现 | Codex | `static/*`、必要 API | 待评审后执行 |

## 编译 / 构建命令

```bash
python -m py_compile app.py
node --check static/quick-nav.js
node --check static/ad-control-pages.js
```

## 风险与依赖
- 用户尚未确认最终页面结构。
- 原型不接真实 API，不代表最终数据加载性能。
- 后续实现必须继续遵守 GitHub-first 和线上备份流程。

## 完成记录
2026-07-01：完成静态原型，等待评审。
