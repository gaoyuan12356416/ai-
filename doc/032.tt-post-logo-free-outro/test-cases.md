# 测试用例

| ID | 场景 | 预期 |
|---|---|---|
| GPU-01 | direct_outro prepare | 最终命令无 Logo 输入与左上角 overlay，仍有片尾和 phone-match 转场 |
| GPU-02 | direct_outro manifest/reuse | manifest v5 只冻结 outro 身份；Logo 文件变化不影响复用，outro 变化拒绝复用/发布 |
| GPU-03 | branded_preview | 显式 Logo 输入仍产生 `scale=132:132` 与 `overlay=48:72`，且不可正式发布 |
| CPU-01 | 手动领取混合 v1/v2 素材 | 跳过先入池的 v1，只领取当前 v2 |
| CPU-02 | 自动预领取/执行 | 三个领取入口都带当前 profile，旧 profile 不会被占用 |
| MIG-01 | 迁移 dry-run | 只返回候选 ID/数量，不调用 GPU、不更新数据库 |
| MIG-02 | 迁移 apply | GPU 身份校验后，pool/intake 同时更新到 v2，状态仍为 available/ready |
| MIG-03 | GPU 返回 profile 漂移 | 拒绝更新，旧账本和成片身份保持不变 |
| MIG-04 | 素材已 reserved 或绑定 run/queue | 不进入候选，不能被迁移覆盖 |
| OPS-01 | 现网迁移 | v1 available=0，v2 available=迁移前数量，历史发布计数不变 |
| OPS-02 | 发布安全 | 部署和验证期间不主动调用发布 runner 或 TikTok publish init |
