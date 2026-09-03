# 开发计划

## 开发范围

详情页独立记录渲染、状态映射、受控轮询及浏览器回归测试。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 独立发布记录 UI | Codex | 两份静态入口 | 已完成 |
| 中文状态与轮询 | Codex | 两份静态入口 | 已完成 |
| 自动化回归 | Codex | `scripts/drama_synthesis_browser.spec.js` | 已完成 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py
node --check <extracted-inline-script.js>
npx playwright test scripts/drama_synthesis_browser.spec.js
```

## 风险与依赖

依赖现有任务详情接口返回 `youtube_publish_tasks`；不依赖真实 YouTube。

## 完成记录

2026-09-03：实现完成，待验证结果回填测试报告。
