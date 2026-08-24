# 测试报告

## 测试结论

本地与服务器回归、生产部署和只读验收全部通过。未创建真实 Meta Post。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Repository + publisher 定向 | 24 | 24 | 0 | 0 |
| FB 自动发布完整回归 | 129 | 129 | 0 | 0 |
| X/TT 合并基线 | 66 | 66 | 0 | 0 |
| 生产部署验收 | 4 | 4 | 0 | 0 |

## 缺陷情况

尚无确认缺陷。

## 验证证据

- `python -m py_compile features/fb_auto_posts/repositories.py features/fb_auto_posts/publisher.py`：通过。
- `python -m unittest scripts.test_fb_auto_repositories scripts.test_fb_auto_publisher`：24 项通过。
- `python -m unittest discover -s scripts -p "test_fb_auto*.py"`：129 项通过。
- X/TT 四个 contract 测试模块：66 项通过。
- 生产只读字段核对：`status` 为 NOT NULL、默认 0；总计 21,210 行，NULL=0。
- 生产组 62：总 Page 13、旧口径可发 8、新口径可发 12、被封 1。
- GitHub/生产 release：`d2a6e91f83ec34f188f41c5d8abb413b0bc1d2b5`；
  服务器侧 FB 129 项、X/TT 66 项再次全部通过。
- 有效备份目录：`/mnt/data-disk/fb-auto-post-deploy/backups/20260824-104651-pre-d2a6e91`；
  operational/metric SQLite 均 `quick_check=ok`，七项 SHA-256 校验通过。
- 切换前危险任务状态为 0；切换前后 run/task/attempt/ledger 均为
  `21/261/277/105`，证明验收没有额外 Graph 发布尝试。
- 生产 repository 实测组 62 为 13/12；五个关注 Page 候选 Token 行数为
  `6/6/6/0/5`，其中 0 对应唯一 `status=1` Page。
- 服务 active/running、`NRestarts=0`、health 正常、启动后无 warning；七个 FB timer 全部 active。

## 遗留风险

- 既有 run 17~21 是冻结审计快照，仍保持 8 可发/5 跳过；不做历史回填。
- 发布验证不创建真实 Meta Post；以 release 测试、只读 Page 池、health、日志和状态机不变量验收。

## 发布建议

已发布，建议保持当前 release。若出现资格口径相关异常，按 `deploy.md` 原子切回
`af1c3b1f52054dd0ad42b00e1e5e8591b4ffe16f`，不得恢复旧 SQLite。
