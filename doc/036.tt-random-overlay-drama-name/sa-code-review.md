# SA 代码审查

## 结论

通过，可进入部署验证。

## 审查要点

- 新模式独立命名为 `random_overlay`，没有删除或改写 `source_direct`、`direct_outro` 和旧片尾实现。
- CPU 两套入口、GPU profile 和 `trim=0` 使用成对校验，配置错配会启动失败或 prepare 失败。
- 资产目录、manifest 和每个文件均校验 SHA-256；渲染前复制到任务临时目录并再次校验，避免渲染过程中资产漂移。
- 配方完全由不可变任务身份和资产集 SHA 派生并写入 manifest；相同 `job_id` 重试复用，不依赖进程随机状态。
- FFmpeg filter 文本不接受请求输入；旋转、缩放、透明度均来自有界整数配方。
- `{{drama_name}}` 只在配置语法检查阶段允许 defer，实际任务冻结时缺剧名会失败关闭。
- 大体积二进制资产不进入 Git，只提交可复现构建脚本、运行时契约和测试。

## 回滚审查

回滚只切 exact Git release 和三方配对 env，不恢复包含新发布事实的数据库或 ledger；旧 profile 和旧片尾资产继续保留。
