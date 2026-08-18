# 测试报告

## 测试结论

通过；已部署并通过独立 QA，P0/P1/P2 均为 0。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 完整 X unittest discovery | 436 | 435 | 0 | 1（Windows symlink 权限跳过） |
| `test_x_posts.py` | 43 | 43 | 0 | 0 |
| `test_x_post_daily.py` | 62 | 62 | 0 | 0 |
| `test_x_post_material_pool_selector.py` | 18 | 18 | 0 | 0 |
| `test_x_post_material_pool.py` | 10 | 10 | 0 | 0 |

## 缺陷情况

发现并修复 2 个代码评审问题：图片零时长 W2A 边界、历史通用错误不可直接非阻断。见 `bugs/BUG-001.md` 和 `sa-code-review.md`。

## 验证证据

- 完整命令：`python -m unittest discover -s scripts -p "test_x_post*.py" -v`。
- 结果：`Ran 436 tests ... OK (skipped=1)`；跳过项仅为 Windows 无 symlink 权限。
- 图片完整 mock 发布验证：下载 JPEG、ffprobe 分支、`tweet_image` initialize、一次 mock create Post、`af_channel=short`。
- 所有 X HTTP 均为离线脚本化响应，没有真实 Post。
- 生产服务器聚焦回归 148 项全部通过（selector 18、pool 10、service 43、daily 62、relay 15）。
- 生产源库只读样本确认活动图片和软删除视频分别返回 `image`、`video`，无 rejection。
- 历史 87 条通用错误完成 selector 重检，79 条恢复，8 条精确拒绝；重检只更新素材校验审计，不建计划、不下载媒体、不调用 X。
- 独立 QA：10 项定向回归通过；JPG/PNG/WEBP/GIF 真实 ffprobe、selector 正反例、3 组媒体类型错配、软删除视频正常 probe/零 repair 对抗矩阵全部通过。

## 遗留风险

- 已删除视频如果 URL/文件本身已经失效，仍会在下载预检阶段失败，这是预期保护。
- 生产现存通用错误需要由新 selector 重检后才会从“不可用”变为“可用”，不会在未核实来源时直接放行。

## 发布建议

建议 immutable release + main exact-file overlay；上线前备份，暂停并恢复现有 timers，只做 health/ledger/自然时隙观察。
