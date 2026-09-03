# 测试用例

- 拒绝非 loopback executor、空 Token、非 Google upload host。
- 香港缓存 SHA/size 漂移必须 unknown，禁止上传；cleanup 只删指定 task。
- 原有 YouTube 幂等、unknown、comment、canary 与 GPU 合成回归通过。
- 生产 health：未授权 401、授权 200、仅监听 loopback。
- 切换不新增 publish row；活动队列为零；仅一个 CPU coordinator 消费 SQLite。
