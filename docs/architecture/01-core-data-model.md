# 01 · Core 数据模型与写入契约

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：作用域模型、Core 动词清单、记忆单元 schema、写入触发、去重/冲突/版本化、并发纪律

## 1. 作用域模型

**bank 不用来分隔 agent**（否则各 Agent 各一个 bank 就不共享了）：

1. **默认只开一个"共享 bank"**，所有 agent 落同一库——这才实现"共同读写"。
2. **行级 `scope ∈ {shared(默认), private}`**：`shared` 全 agent 可读；`private` 仅写者 `agent_id` 可读。agent 之间靠它 + `agent_id` 区分。
3. **`tags[]`**（项目/主题）：只用于检索过滤，**不是访问边界、不承载分类**（分类由 `kind` 承担）。
4. **bank 仅作可选隔离**（不同用户/项目/合规边界），不是默认，也不用它分 agent。

- `agent_id` 恒记录（provenance/审计）：MCP token 名 → agent_id；宿主绑定传自身身份。
- 召回过滤：`scope=shared OR (scope=private AND agent_id=self)`。
- **不含 `session`**（宿主概念挡在绑定层；需要时映射为 tag 或 private）。

## 2. Core 动词清单（11 个，均可序列化成 MCP 工具）

| 阶段 | 动词 | 说明 | 关键入参 |
|---|---|---|---|
| 写入 | `capture` | 原始逐条写入（**跳过写闸门**，用于基准逐轮/原始语料；精确去重可选） | content, kind, scope, topic |
| 写入 | `remember` | 写一条记忆单元（**写前必检索 → 去重/冲突/版本化**，见 §5）；未给 importance 时取该 kind 默认重要性 | content, kind, scope, tags, importance |
| 写入 | `update` | **版本化更新**：生成新版本、旧版转历史（默认保留 `version_keep` 份），非覆盖（§5） | id, fields |
| 写入 | `forget` | **归档**（status=deleted；硬删 `purge` 不在当前范围） | id |
| 读取 | `recall` | 检索（BM25+CJK n-gram + Dense + RRF + 可选 rerank，按 scope 过滤） | query, top_k, filters |
| 读取 | `get` | 按 id 取正文（标题卡→正文展开） | id |
| 读取 | `list` | 浏览/分页（按 tags/scope/time 过滤） | filters, page |
| 关系 | `reviews` | 列出待复核记忆（`status=inactive`） | filters |
| 关系 | `review_resolve` | 裁决（接受/合并/拒绝/取代） | id, resolution |
| 元 | `stats` | 计数/健康 | — |
| 元 | `consolidate` | 只读报告：待定稿候选簇 / 待整理行数（真正定稿由闲时 `run_topic_task` 拉取执行） | dry_run |

## 3. 记忆单元 schema

**不引入知识图谱**——无实体三元组、无多跳遍历、无独立边表。关系用**少量单向指针列**表达。每条记忆**一行、独立检索/向量化**，chunk 小而短，不因关系变大。

**分类 `kind`（单一必填，4 类）**：`persona`（用户偏好/画像）· `fact`（事实）· `event`（过去发生的事）· `procedure`（做法/流程）。

**`tags`**：自由标签，只作检索辅助过滤（项目名/模块名等），不承载分类、不是权限边界。

**表 `memories`（平铺单表）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 当前版本行的唯一 id |
| `root_id` | TEXT | 逻辑记忆稳定标识（同一条记忆所有版本共享；首版 root_id=自身 id） |
| `version` | INTEGER | 版本号，从 1 递增 |
| `agent_id` | TEXT | 写者（provenance/审计） |
| `scope` | TEXT | `shared`(默认) / `private` |
| `kind` | TEXT | persona / fact / event / procedure |
| `tags` | TEXT(JSON) | 自由标签，仅检索辅助过滤 |
| `title` | TEXT | 标题（标题卡/召回展示用） |
| `content` | TEXT | 正文（原文，永不改写） |
| `importance` | REAL | 重要性 0–1（排序用） |
| `source` | TEXT | 通道：hook / manual / mcp / import / consolidate |
| `status` | TEXT | active / inactive / superseded / expired / deleted |
| `dedup_hash` | TEXT | 规范化内容哈希，精确去重 |
| `superseded_by` | TEXT NULL | 被哪个新版本取代（旧版填；active 恒为 NULL） |
| `conflict_of` | TEXT NULL | 冲突对端 id（新条 `inactive` 指向旧条） |
| `created_at` | TIMESTAMP | 创建时间（本版本写入；同时是时间归一的逻辑基准） |
| `updated_at` | TIMESTAMP | 最近更新时间 |
| `last_recalled_at` | TIMESTAMP NULL | 最近被召回时间 |
| `recalled_times` | INTEGER | 被召回次数 |
| `valid_from` / `valid_until` | TEXT NULL | 有效窗口；`valid_until` 兼作到期 → 过期置 `expired` |
| `embedding` | BLOB NULL | 向量（float32 打包，纯标准库） |
| `topic` | TEXT NULL | LLM 定稿主题（每条唯一；生成时为空） |
| `cluster_id` | TEXT NULL | 聚类 id（生成时为空，预聚类时生成） |
| `cluster_stats` | TEXT | unclustered / pre_clustered / clustered |
| `pending_since` | TEXT NULL | 未整理自动捕获行的时间戳（供 TTL 清扫） |
| `episode_id` | TEXT NULL | 指向 `episodes.id`（上下文出处） |
| `normalized_text` | TEXT NULL | 写入时按逻辑时间归一后的正文（相对日期补绝对日期，如 `上周五(2026-06-05)`）；原文 `content` 不变；用于向量/FTS 索引，并随召回结果返回供下游使用。关闭 `time_normalize` 时为 NULL |

**辅助表**
- `memories_fts`：对 `title + (normalized_text or content)` 做 CJK n-gram 分词建 FTS5（检索脊柱）。归一开启时索引归一文本，使"6月5日"与"上周五"都能命中。

**状态语义（5 态）**：`active`=当前有效（唯一参与召回）；`inactive`=创建/入库时判为"待复核"（不进召回，裁决后转 active 或 deleted；只由创建/入库产生，update/召回路径不产生）；`superseded`=被新版本取代的历史版本（可查历史）；`expired`=过了 `valid_until`；`deleted`=软删除/归档（保留行，不召回）。

**关系如何落到指针（无图）**：
- **重复**：`dedup_hash` 精确 + 写闸门 Dice 近重复 → 不新建，保留原文。
- **更新/替代**：版本化 + `superseded_by` 链（§5）。
- **冲突/待复核**：新条以 `status=inactive` 入库并 `conflict_of` 指向旧条，旧条保持 active；`inactive` 不进召回。
- **补充**：同类共存即可，不需要边。

## 4. 写入触发（按宿主能力分流）

1. **有钩子的宿主 → 压缩钩子驱动**：`sync_turn` 不写记忆；压缩时 `post_compaction` → `ingest_compaction` 把累积摘要存为 episode，并由 LLM 比较"这次 vs 上次"抽取增量记忆（`remember`）。原文（宿主 state.db）不属于记忆体系，只在压缩时只读引用摘要。详见 [08 · 分层记忆](08-layered-capture.md)。
2. **无钩子的宿主 → 走 MCP**，由模型自己判断后调 `remember` 等工具。
3. **新 Agent 同此原则**：有钩子就单独设计绑定（捕获压缩摘要），没有就走 MCP。
4. **Core 不假设"自动"**：Core 只认 `remember` / `capture` 被调用；"自动捕获"是绑定层实现细节。

## 5. 去重 / 冲突 / 版本化（确定性，零 LLM）

**写入前置**：`remember` / `update` 都先检索已有相关记忆（`dedup_hash` 精确 + 词法 Dice 近似），据结果决策，绝不盲写。

| 情况 | 判定 | 动作 |
|---|---|---|
| 完全相同（hash 命中） | 重复 | 不新建 |
| 极近（Dice ≥ 0.90） | 重复 | 不新建 |
| 同主题、语义不同 | 补充 | 新建为 `active` |
| 同事实、仅数值/日期变了（Dice ≥ 0.82 近重复） | 更新·待复核 | 新条 `status=inactive` + `conflict_of` 指向旧条；旧条不动；进闲时 LLM 复核 |
| 同事实、说法相反（否定极性冲突 / 同标题异正文 / 拿不准） | 冲突·待复核 | 同上：新条 `inactive` + `conflict_of`；不进召回，待复核 |

**版本化更新（不覆盖）**：
- `update` / 新版本 = **新增一行**（`version+1`、同一 `root_id`、新内容），旧行保持原样但 `status=superseded`、`superseded_by` 指向新行。
- 同 `root_id` 下非 active 历史版本仅留最近 `version_keep` 份（默认 5），更早的转 `deleted`（软删，可被清理任务物理回收）。
- **检索默认**：`recall` 只返回 `status=active`；查历史走 `get(history=true)` 或 `list(root_id=…)`；待复核走 `reviews`（仅 `inactive`）。
- **待复核只出现在创建/入库**：`update` 绝不产生 `inactive`；裁决（`review_resolve`）把 `inactive` 转 active（接受/取代）或 deleted（拒绝/合并）。

**异步复核**：写闸门判为 versioned（近重复 ≥0.82）或 inactive（同标题异正文 / 极性相反）时**都不当场改库**——新条一律存 `inactive`、`conflict_of` 记旧条 id。当 `inactive` 待复核数 > `review_trigger_min`（默认 3）时，闲时任务 `run_review_task()`（宿主 LLM、离线）逐条判定：`same && update` → 以旧条 `root_id` 生成新版本（旧条 `superseded`、按 `version_keep` 修剪）；`same && !update`（仅佐证）→ 直接激活新条、旧条不变；`!same` → 独立激活；LLM 失败 → 保留 `inactive` 待下轮。详见 [05 · 主题聚合](05-topic-clustering.md)。

## 6. 并发纪律

SQLite 在 **WAL** 下天然"单写多读"——同一时刻只有一个写者（跨进程亦然），读永不阻塞。无需外部锁服务/写队列：

1. **连接参数**：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout`（默认约 5s，可配）。
2. **写闸门事务**：用 **`BEGIN IMMEDIATE`** 把"检索→去重/冲突判定→落库"整段包在一个事务里——事务持写锁，别的写者插不进来，天然实现"锁内二次复核"，不另加锁。
3. **后台整理不持锁**：LLM 在事务外算完，再开**短事务**写回；`capture` 原始写入同样走短事务、可批量。
