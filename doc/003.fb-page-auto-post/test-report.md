# 测试报告

## 测试结论

通过（本地实现候选）。未连接生产 MySQL，未调用 Meta，未部署。建议仅进入代码评审/closed-gate 部署准备。

## 测试范围

FB validation/repository/store/publisher/app contract、V2 metric/due queue/GPU prepare-only/容量/部署契约，以及上一轮 X accounts、TT auto、X auto 主 API 合并基线。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| FB V2 专项单元/契约 | 92 | 92 | 0 | 0 |
| X/TT 合并基线 | 66 | 66 | 0 | 0 |
| 当前累计证据 | 158 | 158 | 0 | 0 |

## 缺陷情况

BUG-001 SQLite 连接泄漏、BUG-002 metric SQL `ONLY_FULL_GROUP_BY` 1055、BUG-003 GPU 系统 Python 版本不兼容均已修复并回归。无未关闭本地缺陷。

## 验证证据

- `python -m unittest discover -s scripts -p "test_fb_auto_*.py"`：77/77 PASS；`scripts.test_fb_gpu_prepare_worker`：15/15 PASS。
- `scripts.test_x_accounts_app_contract scripts.test_tt_auto_publish_app_contract scripts.test_x_auto_publish_app_contract scripts.test_tt_posts_app_contract`：66/66 PASS。
- `python -m py_compile ... app.py`：PASS。
- `node --check static/quick-nav.js`、navigation JSON 解析：PASS。
- 最终 inline JS、diff 和敏感扫描见本次交付命令输出。

## 遗留风险

- 修复前 SQL 已由生产只读 EXPLAIN 复现 `ONLY_FULL_GROUP_BY` 1055；候选改为分组内的 material ID 长度+字符串数值序后，已完成生产只读 EXPLAIN：不再报 1055，命中 `pss(product,dt,series_code)`，估算 353,504 行；未执行真实刷新。
- Graph v22.0 已对既有视频对象完成只读 `GET fields=id,status` canary，当前解析可识别 `video_status=ready / processing complete / publishing complete / publish_status=published`；没有创建或修改帖子。
- 首发不支持跨产品，服务端固定 Dramawave `app_id=1479/data_source=6/metric_product=Dramawave/platform=0`，未知映射 fail closed。
- GPU NVENC真实单任务耗时尚无本轮基准；默认 `20 jobs/slot` 不得在无基准时上调。
- GPU worker虽已有仓库内prepare-only入口、失败目录保留/清理和真实 COS 合同，实际资产 manifest、COS public-read HTTPS 回源、NVENC 与 Graph 拉取仍待部署前集成，不在本地 fake 覆盖范围。
- GPU processor和prepare unit已统一为串行1任务；尚无GPU目录总字节硬水位，live前必须完成数据盘可用空间门禁/告警验收。

## 发布建议

不建议开启真实发布。完成 GitHub review、实际资产/COS/NVENC 集成与吞吐基准、备份及 live gate=0 部署后，再单独审批真实 Graph 发帖 canary；不得为本轮验收创建真实帖子。
