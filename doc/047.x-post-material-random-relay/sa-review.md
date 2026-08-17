# SA 评审意见

## 结论

通过。采用无 schema 的最小方案：版本化 SHA-256 seed 负责重建配对，queue 负责最终持久冻结；只开放 material schedule relay，避免污染 manual/X Auto/daily/catch-up。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | runner | 随机不得改变 FIFO 候选集合 | 先 FIFO 扫描，再仅 shuffle 同语言目标列表 | 已解决 |
| SA-002 | P0 | OAuth/store | relay 列表展平可能跨语言或覆盖冻结选择 | 当前资格复核后按候选 relay ID 精确匹配；store 再按 queue 语言过滤 | 已解决 |
| SA-003 | P0 | SQLite trigger | 放宽 relay 可能污染 manual/X Auto | material relay 仅在 `schedule_run_id IS NOT NULL` 时允许 | 已解决 |
| SA-004 | P0 | publish state | relay source 成功不能提前标记 pool | `mark_reposted` 同事务调用 `_mark_pool_published` | 已解决 |
| SA-005 | P1 | recovery | 重启/响应丢失不得重抽 | seed 使用不可变 slot；已存在 queue 比较 material delivery/relay 映射 | 已解决 |

## 决策记录

- 不新增 assignment seed/version 列；版本常量进入 seed，queue 是最终审计事实。
- material zero-attempt relay 重选按 queue identity 稳定选择；drama 继续 least-load。
- 不把 `x_long_video_requires_premium` 变为永久 invalid。
- 最新 FIFO 集合优先于补短：随机目标无同语言 relay 时整批失败，长素材保持未绑定。

## PM 修订确认

requirements.md 已纳入 P0 边界、原子性、unknown fence、部署与回滚约束。
