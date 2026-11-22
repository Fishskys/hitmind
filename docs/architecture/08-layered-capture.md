# 08 · 分层记忆、压缩驱动与闲时整理

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：两层记忆模型、两条捕获路径（work 压缩驱动 / chat 游标批次）、闲时可扩展任务、相关表

## 1. 分层模型（两层：episode + memory）

```
原文/转录     宿主 state.db（messages）—— 外部、可选的"尽力而为"来源，非 HitMind 的一层
                 │  work：压缩时只读取"累积压缩摘要"；chat：按游标读取"未处理轮次"
                 ▼
L1 episode    一个 session 一条（session_id 唯一）；work 存压缩摘要，chat 存窗口批次(range)
                 │  抽取记忆
                 ▼
L2 memory     最终记忆（kind ∈ persona/fact/event/procedure），episode_id 指回 L1
```

**核心原则**
- **原文不属于记忆体系**：不复制逐轮原文；`state.db` 只是"能读到就用"的外部来源，读不到时 episode/L2 仍成立。
- **写时不调 LLM**：热路径（`sync_turn`）只记账/打标记；LLM 抽取只在**闲时后台线程**里做。
- **episode 可 TTL 物理删除**（宿主转录镜像、可重建）；L2 记忆不硬删，只归档/过期。
- **每条 L2 记忆带 `episode_id`**。

## 2. 两条捕获路径

### A. work 模式 —— 压缩驱动（first vs delta）
`post_compaction` 钩子 → 读该 session 最新累积摘要 `S_new` → upsert episode（revision+1，summary 原位替换）→ LLM：`prev=None` 全抽 / `prev=S_old` 只抽增量 → 写 memories（挂 `episode_id`）。
> 为什么 delta：宿主摘要是**累积 + 预算内滚动**（有损），整体重抽会重复；比"这次 vs 上次"只抽增量。

### B. chat 模式 —— 游标批次（busy 打标，闲时抽取）
```
sync_turn（热路径，零 LLM）：note_activity(session)
   → 有未处理轮次 >= 2*consolidate_every_turns 且未打标 → pending_batch=1；wake 后台线程
闲时后台线程醒来 → process_pending：
   对每个 idle 且 pending 的 session → process_chat_batch：
     取 [cursor_id, ...] 的真实对话（去 summary 行）→ 一次 LLM 抽取
       成功（含 0 条）→ add_episode(window) + remember×k → 提交游标=最后一条id，清 pending，fails=0
       失败/无LLM     → 保留游标+pending，fails+1（下次重试）
```
- **繁忙只打标、不抽取**：达阈值即 `pending_batch=1`，实际 LLM 在闲时跑。
- **不遗漏**：游标只进不退；成功才推进；`on_session_end` 兜底 flush 尾巴；`rows_after` 用稳定 `id` 作锚。
- **不重复**：游标之后才处理 + 成功才前进 + `remember` 写闸门去重 + `add_episode` 一 session 一行。
- **失败重试**：抽取失败 → 不动游标，`fails+1`；下次 idle 再来。
- **空闲判定**：`now - last_activity >= idle_consolidate_seconds`（默认 300s；0=关闭闲时整理）。

## 3. 闲时可扩展任务（适配器侧轮询线程）

宿主**没有** idle/定时钩子（钩子均为事件驱动）。HitMind 在**适配器侧**自建一个 daemon 轮询线程：每 `idle_poll_seconds`（默认 60）或 `wake()` 时调 `Engine.run_pending_pass()`。

- **可扩展**：任务用 `register_pending_task(name, fn)` 注册，`run_pending_pass` 逐个跑、单个异常不影响其它。当前两个：`chat_batch`（`process_pending`）、`topic`（`run_topic_task`）。
- **串行/单飞**：进程级 `_PASS_LOCK` 保证同一时刻只有一个 pass。
- **不碰热路径**：线程独立、daemon、随 `shutdown()` 停止。

Topic 聚类定稿的拉取式双阈值逻辑见 [05 §3](05-topic-clustering.md)。

## 4. `episodes` 表（一 session 一行）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | `ep_<ULID>` |
| `agent_id` / `scope` | TEXT | provenance / 共享域 |
| `session_id` | TEXT UNIQUE | 一 session 一行；work 首次压缩判定依据 |
| `mode` | TEXT | work / chat |
| `trigger` | TEXT | compaction / chat_batch / manual |
| `summary` | TEXT | work=最新累积摘要；chat=""（窗口无摘要） |
| `summary_kind` | TEXT | compaction / window |
| `message_id_range` | TEXT(JSON) | 处理的 state.db 消息 id 范围 `[first,last]`（chat 窗口用） |
| `revision` | INTEGER | 每次压缩/批次 +1 |
| `status` | TEXT | active / archived / expired |
| `created_at` / `updated_at` | TEXT | 建立 / 最近更新 |
| `valid_until` | TEXT | updated_at + `episode_ttl_days`；到期**物理删除** |

## 5. `sessions` 表（每会话处理状态）

| 字段 | 说明 |
|---|---|
| `session_id` PK | 会话 |
| `cursor_id` | 已处理到的 state.db 消息 rowid（只进不退；成功才推进） |
| `last_activity` | 最近活动时间（ISO），用于 idle 判定 |
| `pending_batch` | 1=有一个待处理 chat 批次 |
| `batch_start` | 批次起点快照（调试/日志） |
| `fails` | 连续抽取失败次数（观察用，不设死限） |
| `updated_at` | 行更新时间 |

## 6. `topic_tasks` 表（topic 定稿的"已处理"记录）

拉取式模型下它不是待办队列，而是记录"这个簇上次定稿时的成员数/状态"，用于避免重复定稿、以及"增长后才重新定稿"。

| 字段 | 说明 |
|---|---|
| `cluster_id` PK | 一簇一行（天然去重） |
| `size` | 上次 `done` 时该簇的 active 成员数（用于"增长后才重新定稿"判定） |
| `cached_members` | 旧缓存字段，拉取式下不再使用（保留列，恒 NULL） |
| `status` | done（已定稿）/ dropped（放弃或非同主题） |
| `fails` | 连续 LLM 失败次数（达上限 → dropped） |
| `created_at` / `updated_at` | 时间戳 |

## 7. kind 与 TTL

| kind | 半衰 | 默认 TTL |
|---|---|---|
| persona / fact | 180 d | 无 |
| procedure | 60 d | 无 |
| event | 14 d | 90 d |
| episode 层 | — | `episode_ttl_days`（默认 90 d）物理删除 |

> **刷新语义**：召回或更新一条有时限 TTL 的记忆时 `valid_until` 顺延 `now+TTL`；`freshness` 的 age 基准 = `max(updated_at, last_recalled_at)`（召回也回鲜）。

## 8. 相关参数

`idle_consolidate_seconds`（默认 300，0 关闭闲时整理）、`idle_poll_seconds`（默认 60）、`consolidate_every_turns`（默认 10；chat 批次按**未处理的真实 user 回合数**计，达到 **2×此值（默认 20）** 才标记待抽取——10 是批大小、20 是触发线，留一批余量；回合数经 `sync_roles`/`sync_user_min` 过滤，user 默认门槛 0=不限）、`cluster_min_size`（默认 10，全局 backlog 门槛）、`topic_min_cluster`（默认 2，每簇够格线）、`episode_ttl_days`（默认 90）。详见 [06 §4](06-engineering.md)。
