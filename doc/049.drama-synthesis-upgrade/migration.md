# 历史 outputs_json 迁移

迁移对象只限 CPU SQLite `drama_material_job.outputs_json`，不触碰 MySQL、短链文件或外部平台。

1. 停止新 drama job 写入并记录当前 Git SHA、DB 路径/大小/权限；确认绝对备份目标不存在且不等于源文件。
2. 运行不带 `--apply` 的 dry-run；脚本以 `mode=ro` 打开 DB，校验精确列和每行 JSON，输出 rows/changes，不创建备份、不写数据。
3. 使用 `--apply --backup <new-absolute-path>`；先用 SQLite online backup API 生成一致性备份并 fsync，再以 `BEGIN IMMEDIATE` 单事务执行带 original-value predicate 的逐行更新。
4. 任一 JSON/schema/concurrency 错误整批 rollback；不得跳过坏行。成功后二次 dry-run 必须 `changes=0`，并执行 integrity check、四键抽样与任务输出读回。
5. 归一规则：已有显式布尔优先；缺失的三个历史普通产物按已存结果 URL 推断；权威 `random_template_video` 缺失固定 false。历史错误 `random_template` 仅作为输入 fallback，迁移后只存 `random_template_video`，绝不采用新任务默认重解释历史。

回滚：关闭新代码写入，核验 backup integrity 后恢复整库备份并恢复原 SHA。若新代码已产生新任务，禁止直接覆盖；先停服、冻结差异并由 owner 决定前向修复。additive 表/列不做反向 DDL。
