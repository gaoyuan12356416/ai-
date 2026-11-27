# 发布机清单和香港安全组

CPU 上的 `fb-page-v2-inventory.timer` 每五分钟查询腾讯云香港地域。发布机属于 VPC `vpc-lg5p8p82`，关联已核实的发布安全组 `sg-lqtd1q1l` 或 `sg-pgzi0n4v`，实例名以“ads发布调控”开头，实例状态为 RUNNING。第二个组目前包含发布机 `10.89.0.14`。清单收录实例的私网 IPv4，排除带临时、测试或预发标记的节点。同 VPC 新子网通过相同身份条件自动纳入。实例移除、停机或角色调整会撤销其地址。

2026-11-27 用户另行授权生产 `ads web`：实例 `ins-q12mp898` 的 `10.89.0.9`。
这一调用方按实例和地址精确纳入，保留同 VPC、RUNNING 核对，不扩大其安全组的访问范围。
应用的 Bearer 和实际发布任务/Page 池权限检查继续执行；专用云安全组只开放 TCP 8944。

服务先原子更新 CPU 清单 `/mnt/data-disk/fb-page-v2/catalog/publishers.json`，再同步香港云规则。香港应用独立拉取同一清单。专用安全组 `fb-page-v2-hk-8944` 有 `managed-by=fb-page-v2` 和 `target-instance=ins-52tbyu6m` 标签。每个发现的地址对应一条 TCP 8944 入站 /32 许可，出站规则为空。CPU 在更新前检查专用组身份、云端关联数量和实例私网地址，在更新后读回规则与绑定。创建途中遇到网络异常，下一轮按标记查找云端对象。未确定是否创建成功的记录保存在 `/mnt/data-disk/fb-page-v2/network/security-group.json`，可配合对应账号的 CloudAudit 查看请求结果。

CPU 已配置服务环境文件后，立即同步和查看状态可使用

```sh
systemctl start fb-page-v2-inventory.service
systemctl enable --now fb-page-v2-inventory.timer
. /etc/fb-page-v2-inventory.env
cd /opt/fb-page-assignment-v2/current
/usr/bin/python3 -B ops/fb-page-v2/network.py status --credentials "$TENCENT_CREDENTIALS_PATH"
```

回滚时从状态输出读取本次的 group_id，执行以下命令，将 SG_ID 替换成读回的值。命令先暂停该组的自动维护，再从 `ins-52tbyu6m` 解除这一个组并读回。下次定时任务继续更新发布机清单。专用组保留，可在重新接入时复用。

```sh
. /etc/fb-page-v2-inventory.env
cd /opt/fb-page-assignment-v2/current
/usr/bin/python3 -B ops/fb-page-v2/network.py detach --group-id SG_ID --credentials "$TENCENT_CREDENTIALS_PATH"
```

重新启用时执行

```sh
/usr/bin/python3 -B ops/fb-page-v2/network.py resume --group-id SG_ID --credentials "$TENCENT_CREDENTIALS_PATH"
systemctl start fb-page-v2-inventory.service
```

腾讯云接口参数和多个安全组的匹配顺序见[创建安全组](https://cloud.tencent.com/document/api/215/15806)、[重置规则](https://cloud.tencent.com/document/api/215/15810)、[绑定实例](https://cloud.tencent.com/document/api/213/31282)和[安全组优先级](https://cloud.tencent.com/document/product/213/112620)。
