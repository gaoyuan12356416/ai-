# 兼容接口

snapshot [--owner TASK]：stdout 仍仅返回路径。
lease PATH --owner TASK [--hours 72]：延长租约，不缩短已有租约。
pin PATH --owner REASON / unpin PATH --owner REASON：显式永久保护开关。
prune [--apply]：默认预览，只管理已登记快照。
retire --manifest AUDIT.json [--apply]：仅显式审计清单，执行时复核保护和身份。
exec -- COMMAND：原有行为不变。

latest.json 的 data_files[level][day].path/row_count 保持，增加 revision/bytes/source_refreshed_at。分区数据增加 source_refreshed_at；manifest 的 generated_at 仍表示本轮发布。无业务 API/DDL 变更。
