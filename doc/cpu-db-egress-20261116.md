# CPU 至数据库丢包：公网出口限速修复

目标 CPU：`43.166.187.96` / `ins-lyr5109j`，`na-ashburn`。
数据库目标：`101.32.56.53:63350/63353`，原只读/写入边界与 SQL Gate 不变。

## 证据与调整

云 API 读回公网出口上限为 20 Mbps、计费为 `TRAFFIC_POSTPAID_BY_HOUR`。
17:43、17:44、17:50、18:21 故障窗口出带宽接近或达到上限；18:21 的
`WanOuttraffic` 为 21.33 Mbps、`Outratio` 为 101.712%。网卡、softnet、
qdisc drop/overlimit 计数均为 0，连接追踪、主机和数据库连接资源有余量。
此前 TCP 头部记录已确认出方向数据段缺失/SACK/重传，导致大日志传输超时。
这些证据指向 CPU 实例的云端公网出口限速层，而不是仅凭中间跳 ICMP 丢包指认路由器。

将该实例带宽上限 20 → 100 Mbps，保留按 GB 的流量计费、原公网 IP、实例规格和运行状态。
两个带宽档位必须读回相同的 GB 单价；调整本身不新增固定带宽费，实际出网流量仍计费。
不重启实例或业务服务，不重置发布队列、outbox、Token 或回执。

先提交到 GitHub，在 CPU 干净 checkout 拉取精确版本，执行只读计划并保留其输出；
`apply` 在数据盘保存变更前配置、原/新询价、请求 ID 和生效读回。
未收到云端写入回执时仅轮询原实例配置，不盲目重发或自动回滚未知写入。

```bash
python3 CHECKOUT/ops/cpu-db-egress/egress.py plan
python3 CHECKOUT/ops/cpu-db-egress/egress.py apply \
  --deployment /mnt/data-disk/cpu-db-network/deployments/UNIQUE_DEPLOYMENT
```

回滚只恢复该实例的带宽上限 100 → 20；如其他操作者已改变带宽/计费模式则保留现状。

```bash
python3 CHECKOUT/ops/cpu-db-egress/egress.py rollback \
  --deployment /mnt/data-disk/cpu-db-network/deployments/UNIQUE_DEPLOYMENT
```

## 验证

比较调整前后的原主键、同内容重复 INSERT 传输探针（预期 1062，原记录不变）、
TCP 重传、云端带宽/利用率，以及真实 Page 分配 HTTP 和 MySQL 日志/原始 JSON hash。
同时核对 Page 服务 PID/代码指针未变、历史响应/回执完整、outbox 无新增错误。
不使用长时间满速压力测试；不把短时成功样本外推为永久零丢包。

官方依据：
- [监控指标](https://cloud.tencent.com/document/product/248/6843/)
- [超出带宽上限默认丢包](https://cloud.tencent.com/document/product/213/17282)
- [按流量计费的带宽调整](https://cloud.tencent.com/document/product/213/10580)
- [实时调整 API](https://cloud.tencent.com/document/product/213/15721)
