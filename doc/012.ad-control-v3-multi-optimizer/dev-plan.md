# 开发计划

## 开发范围

在生产提交 `a4dad6d2ff708b04a434945b5c18e9f6caf2fdef` 上建立独立 worktree，增量实现多优化师身份、关系表、扫描合并和动态 UI 展示。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产只读诊断 | Codex | 日志、SQLite、源 MySQL | 完成 |
| 身份与服务层 | Codex | catalog.py / service.py | 完成 |
| 多对多存储 | Codex | repository.py / SQL 004 | 完成 |
| 执行审计 | Codex | live_execution.py | 完成 |
| 动态 UI | Codex | assets/app.js | 完成 |
| 自动化测试 | Codex | tests/test_ad_control_v3_* | 进行中 |
| GitHub-first 部署 | Codex | GitHub / 43.166.187.96 | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile features/ad_control_v3/*.py
python -m unittest discover -s tests -p "test_ad_control_v3*.py" -q
node --check features/ad_control_v3/assets/app.js
git diff --check
```

## 风险与依赖

- 依赖 ads_ai 写库 63353 可用，且先成功创建关系表。
- 依赖源库 63350 的 admin_user_group/admin_users 精确身份查询。
- 发布前必须再次核对线上文件 hash 与源提交，避免覆盖共享 monolith 的其他变更。

## 完成记录

- 已创建本地源代码压缩备份，SHA256 `C6214248FDFA70CDC715D2ED1314FF9E396369E01CA18EBDC45B5562D4418532`。
- 已完成核心、存储、UI 定向回归；最终结果见 test-report.md。
