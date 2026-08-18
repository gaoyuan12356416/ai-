# 测试报告

## 测试结论

通过。已完成本地161项回归、CPU/GPU closed-gate生产部署、30日指标回填和prepare-only GPU→COS canary；未调用 Meta，真实发布仍保持关闭。

## 测试范围

FB validation/repository/store/publisher/app contract、V2 metric/due queue/GPU prepare-only/容量/部署契约，以及上一轮 X accounts、TT auto、X auto 主 API 合并基线。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| FB V2 专项单元/契约 | 95 | 95 | 0 | 0 |
| X/TT 合并基线 | 66 | 66 | 0 | 0 |
| 当前累计证据 | 161 | 161 | 0 | 0 |

## 缺陷情况

BUG-001 SQLite 连接泄漏、BUG-002 metric SQL `ONLY_FULL_GROUP_BY` 1055、BUG-003 GPU 系统 Python 版本不兼容、BUG-004 oneshot 忽略 `RuntimeMaxSec`、BUG-005 MySQL/Python跨content排序语义不一致、BUG-006 COS SDK自定义元数据键缺少协议前缀均已修复并回归。无未关闭本地缺陷。

## 验证证据

- `python -m unittest discover -s scripts -p "test_fb_auto_*.py"`：80/80 PASS；`scripts.test_fb_gpu_prepare_worker`：15/15 PASS。
- `scripts.test_x_accounts_app_contract scripts.test_tt_auto_publish_app_contract scripts.test_x_auto_publish_app_contract scripts.test_tt_posts_app_contract`：66/66 PASS。
- `python -m py_compile ... app.py`：PASS。
- `node --check static/quick-nav.js`、navigation JSON 解析：PASS。
- 最终 inline JS、diff 和敏感扫描见本次交付命令输出。
- 生产页面：模板页与发布记录页均HTTP 200；已登录DOM显示视频制作模板=随机排重模板、Page池/素材条件/固定或每日随机频率；管理API匿名访问401。
- 生产闭锁：scheduler/plan/prepare/runner/reconcile自然timer均返回 `live_gate_closed`，六张发布操作表始终为0；sidecar/main API/GPU/tunnel `NRestarts=0`。
- 指标：2026-07-19至2026-08-17共30个active READY pointer、669,299日明细行、0 building，两个SQLite `quick_check=ok`。
- GPU/COS：首次17.218秒、二次0.002秒复用；公开HEAD 200、`video/mp4`、3,306,811 bytes，SHA/profile元数据一致；成功job仅manifest，GPU数据盘101G可用。

## 遗留风险

- 修复前 SQL 已由生产只读 EXPLAIN 复现 `ONLY_FULL_GROUP_BY` 1055；候选修复后生产只读 EXPLAIN、单日23,765行和最近30日完整刷新均通过。content ID 使用binary canonical排序，素材ID使用ASCII正整数长度+字符串数值序。
- Graph v22.0 已对既有视频对象完成只读 `GET fields=id,status` canary，当前解析可识别 `video_status=ready / processing complete / publishing complete / publish_status=published`；没有创建或修改帖子。
- 首发不支持跨产品，服务端固定 Dramawave `app_id=1479/data_source=6/metric_product=Dramawave/platform=0`，未知映射 fail closed。
- GPU已有单任务基准，但尚无20任务/同槽连续吞吐基准；默认 `20 jobs/slot` 不得上调。
- 实际资产manifest、COS public-read HTTPS回源与NVENC已通过；Graph真实发帖仍未执行，需另行明确审批。
- GPU processor和prepare unit统一为串行1任务；当前101G可用满足40G静态门禁，但持续磁盘告警与live前水位复核仍需完成。

## 发布建议

closed-gate 部署已完成，不建议开启真实发布。开 live 前仅剩持续磁盘告警/当时水位复核、同槽连续吞吐评审，并须单独审批真实 Graph 发帖 canary；不得把本轮验收当作真实发帖授权。
