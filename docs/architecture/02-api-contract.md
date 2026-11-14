# 02 · 接口契约（Core API）

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：通用约定、Memory 返回形状、动词 spec、错误码、Hermes 绑定、MCP 暴露

## 1. 通用约定

- **ID**：`mem_<26位小写base32>`（ULID 风格，时间有序）；版本行各自 `id` 不同，共享 `root_id`。
- **时间**：一律 ISO-8601 UTC 字符串。
- **返回信封**：成功 `{"ok": true, "data": <T>}`；失败 `{"ok": false, "error": {"code","message","detail"}}`。Python 层可 `raise HitMindError(code,...)`，由 adapter 统一转信封。
- **`scope`**：`shared`(默认) / `private`；`private` 仅写者 `agent_id` 可读写，他人调用返回 `FORBIDDEN`。
- **`agent_id`**：字符串；宿主绑定默认自身身份，MCP 由 `--agent-id` / `HITMIND_AGENT_ID` 提供。
- **分页**：`limit`(默认 50, 上限 500) + `offset`；返回 `next_offset`（无更多为 null）。

## 2. 记忆对象（Memory）返回形状

```
{ id, root_id, version, agent_id, scope, kind, tags[], title, content,
  importance, source, status,
  created_at, updated_at, last_recalled_at, recalled_times,
  valid_from, valid_until, superseded_by, conflict_of,
  topic, cluster_id, cluster_stats, pending_since, episode_id, normalized_text }
```
`recall` 结果额外带 `scores: { dense, bm25, rel_norm, freshness, final }`（启用 rerank 时另带 `rerank`）；命中带 `episode_id` 时附 `episode_summary`。
自动注入（宿主 prefetch）每行渲染为 `[id] (kind)[@created_at日期]：正文`（含 `id/kind/created_at/content`，受 token 预算）。

## 3. 动词 spec

**① `capture(content, kind, *, title?, tags?, scope="shared", importance=None, source="hook", topic?, skip_if_duplicate=false) -> {id}`**
原始逐条写入，**跳过写闸门**（用于基准/原始语料/逐轮捕获）；`skip_if_duplicate=true` 时同 scope 精确重复返回既有 id（Core 级去重）。`importance` 未给则取该 kind 默认重要性。

**② `remember(content, kind, *, title?, tags?, scope="shared", importance=None, source="manual", valid_from?, valid_until?, episode_id?) -> {action, id, root_id, version, status, conflict_of?, warning?}`**
先跑写闸门（[01 §5](01-core-data-model.md)）。`action ∈ {created, duplicate, inactive}`：
- `created`：`status=active` 新行；
- `duplicate`：不新建，返回命中的既有 `id`；
- `inactive`：判为近重复（≥0.82）或冲突——不立即版本化，新条存 `status=inactive` + `conflict_of`，交闲时 `run_review_task` 判定是否以旧条 `root_id` 生成新版本。

**③ `update(id, *, content?, title?, importance?, tags?, valid_until?) -> {action:"versioned", id, root_id, version}`**
**永不原地覆盖**：生成新版本行，旧行 `superseded`。不接受改 `kind/scope/agent_id`（那是新建）。

**④ `forget(id) -> {id, status:"deleted"}`** — 软删（`deleted`，保留行）。

**⑤ `recall(query, *, top_k=5, filters?) -> {results:[Memory+scores], count}`**
`filters = {kind?:[], tags?:[], scope?:, root_id?:, updated_after?:, valid_at?}`。默认只召回 `status=active` 且通过 scope 过滤；**无相关 → `results: []`（不报错）**。流程见 [03 · 检索栈](03-retrieval.md)。

**⑥ `get(id, *, history=False) -> {memory(s)}`** — `id` 精确取一行；`history=True` 返回同 `root_id` 的版本链（含 superseded）。

**⑦ `list(*, filters?, limit=50, offset=0) -> {items, total, next_offset}`** — 浏览；`filters` 同上，另可 `status`（默认不含 deleted）。

**⑧ `reviews(*, limit=50) -> {items:[Memory], count}`** — 列 `status=inactive`。

**⑨ `review_resolve(id, resolution) -> {id, status}`**
`resolution ∈ {accept, reject, supersede}`：`accept` 转 active；`reject` 转 deleted；`supersede` 作为新版本使旧条 `superseded`、自身 active。

**⑩ `stats() -> {counts:{by_status,by_kind}, db_bytes, embedding:{ok,model,dim}}`** — 只读健康信息。

**⑪ `consolidate(*, dry_run=True) -> {dry_run, working_pending, min_working, qualifying_clusters, llm_enabled}`** — 只读报告：统计待定稿候选簇与待整理行数；真正定稿由闲时 `run_topic_task` 拉取执行。

## 4. 错误码

`INVALID_ARGUMENT` / `NOT_FOUND` / `FORBIDDEN`（private 他人）/ `DB_BUSY`（写锁超时）/ `NOT_IMPLEMENTED` / `INTERNAL`。
`EMBEDDING_UNAVAILABLE` 不是错误：降级 BM25-only，在 `stats`/响应 `degraded: true` 标记。重复命中不是错误（`remember` 返回 `action=duplicate`）。

## 5. Hermes 绑定映射（MemoryProvider）

| MemoryProvider | → Core |
|---|---|
| `name` | `"hitmind"` |
| `is_available` | Engine 可导入且 `db_path` 可解析 |
| `initialize(session_id, hermes_home, agent_context, ...)` | 打开 `Engine(db_path=$HERMES_HOME/hitmind/data/hitmind.db)`，`agent_id` 取自身身份；非 primary 上下文只读/跳过写；注册闲时任务并启动后台线程 |
| `system_prompt_block` | 静态 guide（标题卡/工具用法），无动态内容 |
| `prefetch(query)` | `recall(query, top_k, filters=active+scope)` → **裸**记忆片段列表（每行 `[id] (kind)[@created_at日期]：正文`，受 token 预算），有超时上限；**不加 `<memory-context>` 围栏**（由宿主 `build_memory_context_block` 统一包裹，provider 预加会被剥掉并告警）；超时/不可用走 BM25-only 或返回空 |
| `queue_prefetch` | 预热查询向量/缓存（可为 no-op） |
| `sync_turn(user, assistant, messages)` | **不写记忆**（热路径零写）；仅 `note_activity` + 唤醒后台线程；压缩时由 `post_compaction` 触发 `ingest_compaction` |
| `get_tool_schemas` | 默认暴露 9 个：②③④⑤⑥⑦⑧⑨⑩（**不含 capture**；`consolidate` 为派发专用、不暴露）；`export`/`import` 仅显式开关时额外追加 |
| `handle_tool_call` | 转 `Engine` 分发，返回 JSON 字符串信封 |
| `shutdown` | 关连接（WAL checkpoint）、停后台线程 |

> **写入触发**：Hermes 走**压缩钩子**——`sync_turn` 不写；`post_compaction` → `ingest_compaction` 把累积摘要存为 episode，LLM 比较"这次 vs 上次"抽增量记忆。原文不属于记忆体系。无钩子宿主走 MCP 显式 `remember`。详见 [08](08-layered-capture.md)。

## 6. MCP 暴露

- 工具集默认 9 个（`remember/recall/get/update/forget/list/reviews/review_resolve/stats`；capture/consolidate 不暴露；`export`/`import` 需显式开关）。
- **传输**：当前实现为 **stdio**（`hitmind-mcp` / `python -m hitmind.mcp.server`），`agent_id` 取自 `--agent-id` 或 `HITMIND_AGENT_ID`（默认 `mcp`）。**streamable-HTTP + bearer + token→agent_id 尚未实现**（规划项）。
- 工具返回 = §3 的 JSON 信封（字符串化）。

## 7. 当前范围之外

图/多跳、跨库召回、硬删(`purge`)、向量索引 ANN 优化。（rerank 已实现为二级精排，见 [03](03-retrieval.md)。）
