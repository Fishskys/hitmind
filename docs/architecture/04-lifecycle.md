# 04 · 生命周期与规模控制

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：kind 差异化衰减、freshness/综合分、容量与 TTL

## 1. 规则

1. **kind 差异化指数衰减 → 只影响 `freshness` 分**，不改原始相关性：
   `freshness = 0.5 ** (age_days / half_life[kind])`，其中 **age 基准 = `max(updated_at, last_recalled_at)`**（更新与召回都会"回鲜"）。
2. **最终召回分 = 加权归一化**（默认 `w_rel=0.8`、`w_imp=0.1`，可配）：
   `final = w_rel · rel_norm + w_imp · importance + (1 − w_rel − w_imp) · freshness`
   即默认 **0.8×相关度 + 0.1×重要性 + 0.1×新鲜度**：相关性主导，重要性与新鲜度各占轻权重，只在相关性相近时微调次序。
3. **`recalled_times` / `last_recalled_at`**：每次召回 `+1` 并刷新 `last_recalled_at`；`last_recalled_at` 参与 `freshness` 的 age 基准，但 `recalled_times` 本身不进排序。
4. **容量上限设大、只告警不删**：超过阈值只产生告警/统计，不自动归档、不自动删。
5. **默认 TTL**：`event` = 90 天；`persona` / `fact` / `procedure` 默认不过期（可显式给 `valid_until`）。到期置 `expired`（读时过滤，不删）。
   - **刷新语义**：召回或更新一条有时限 TTL 的记忆时，`valid_until` 顺延为 `now + kind TTL`（"在用就不该过期"）；无限期 kind 无 `valid_until`，仅靠 `freshness` 衰减。
6. **衰减/过期都不删数据**：只有显式 `forget`（→`deleted`，软删）会动行（硬删 `purge` 不在当前范围）。

## 2. kind 差异化参数（默认值）

| kind | 半衰 `half_life` | 默认 TTL |
|---|---|---|
| `persona` | 180 d | 无（长期画像） |
| `fact` | 180 d | 无 |
| `procedure` | 60 d | 无 |
| `event` | 14 d | 90 d |

（理由：persona/fact 是稳定事实，衰减最慢、不设过期；procedure 中等；event 是"过去发生的事"，时效性最强、衰减最快。）

## 3. 落库与计算

`freshness` 与 `final` 都是**计算值**（不存表）；`half_life` 按 kind 配置在代码常量（可环境/配置覆盖）；age 用 `max(updated_at, last_recalled_at)` 计算。
