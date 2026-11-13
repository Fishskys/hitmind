# 05 · 主题聚合

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：cluster_id/topic/cluster_stats 字段模型、写时预聚类、闲时 LLM 定稿、召回扩展、可调参数

## 1. 字段模型

| 字段 | 含义 | 生成时机 |
|---|---|---|
| `cluster_id` | 聚类 id（一组"疑似同一件事"的候选成员） | 生成记忆时为空；预聚类时生成（当前：写入即预聚类） |
| `topic` | **LLM 定稿的 topic**，每条记忆唯一；同簇成员共享同一 topic | 生成记忆时为空；由 `finalize_cluster(cluster_id, topic)` 写入 |
| `cluster_stats` | `unclustered`（孤立单条：size=1 或假组解散后的单条；`cluster_id`=自身）/ `pre_clustered`（真正的候选组：≥2 条、topic 仍空、待定稿）/ `clustered`（已定稿，有 topic） | 随上面两步迁移 |

`topic` **只能由 LLM 定稿**产生，不使用符号词典打标。

## 2. 写时预聚类（零 LLM）

`capture` / `remember` 后立即跑单链接预聚类：**余弦门 `cluster_cos_floor`（默认 0.78，宁滥勿缺：同主题尽量并）、无并查集、单簇上限 `topic_max_members`（默认 10）**，只把新记忆挂到"最近 `cluster_scan_window`（默认 200）条里最像的那一条所在簇"。

- 新建孤立单条置 `unclustered`（`cluster_id`=自身）；当有第 2 条并入某簇时，整组升为 `pre_clustered`。
- 若并进的簇已 `clustered`（定稿过），新成员**直接继承其 topic、置 `clustered`**（不重新定稿、不 churn）。
- 写时**不入队**，入队交给闲时拉取扫描。
- 不做传递合并（避免把全库并成 1 簇）。

## 3. 闲时 LLM 定稿（拉取式 + 双阈值解耦）

由闲时 `run_topic_task()` **拉取式**发现并定稿，两级门槛解耦：

- **全局积压门槛**：全部 active `pre_clustered` 成员数合计 ≥ `cluster_min_size`（默认 10，可配）才启动一轮整理。
- **每簇够格线** `topic_min_cluster`（默认 2）：成员 ≥2 的簇才送 LLM 判同一件事。
- **每轮上限** `CONSOLIDATE_BATCH_MAX=20` 簇 → 单轮 LLM 读入 ≤ 20×`topic_max_members` 条，有界。
- 离线 LLM 读整簇原文判定是否同一件事并给出 topic / corrections → `finalize_cluster` 写 topic、置 `clustered`。**LLM 调用按簇而非按轮**；`consolidate_llm_enabled`（默认开）。
- **为何解耦**：旧口径"每簇 ≥ `cluster_min_size` 才入队"叠加单簇上限，会导致只有接近上限的簇能定稿、小簇永久饿死。解耦后任何 ≥2 条的簇都可定稿，"10"改为**全局积压**门槛。
- **实现**：`adapters/hermes/llm.py` 的 `HostClusterLLM` —— **只用宿主提供的 LLM 接口（`ctx.llm`）**，HitMind 不直连 HTTP、不读 API key。走结构化 JSON 输出，路由到插件注册的 auxiliary task **`hitmind_topic`**（用户填了该 task 的 provider/model 就用之，未填自动回落主模型）。`run_finalize_pass` 是**唯一打破"写时零 LLM"的地方**，只离线/显式调用，不进每轮；`same_matter=false` 的簇置回 `unclustered` 防重排队。

## 4. 矛盾/更正（LLM corrections）

`finalize_cluster` 提示 LLM 在判同一件事时**顺带检查彼此矛盾/前后更新**，返回 `corrections:[{index, corrects, note}]`（`index`=较新的更正条、`corrects`=被更正的旧条）。落库时：① 把 `corrects` 指向的旧条标 `superseded`（**不删除**，可用 `get` 追溯）；② 给更正条正文前缀 `[更正] <note>`（并重建 search_text）。写闸门 `classify()` 仍是第一道确定性冲突防线，LLM corrections 补的是**跨 title、语义级**的更新。

## 5. 更新/冲突的异步复核

写闸门判为 versioned / inactive 的新条存 `inactive`（见 [01 §5](01-core-data-model.md)）。当 `inactive` 待复核数 > `review_trigger_min`（默认 3）时，闲时任务 `run_review_task()`（宿主 LLM，离线）逐条判定是否同一件事并决定是否以旧条 `root_id` 生成新版本。

## 6. 召回扩展

取 RRF 融合结果**前 `topic_expand_hits` 条记忆**（固定前缀，无论这几条是否同主题）的 `topic`，对每个 topic 仅在 `cluster_stats='clustered'` 时扩展"同 topic 的其它 active 记忆"；每个被展开 topic **总共最多保留 `topic_expand_cap` 条**（**已命中的那条也计入该额度**，最新优先），**去重后不再补足**。`pre_clustered`/`unclustered` 一律不展开。默认 hits=3、cap=3 → 每主题最多再带出 2 条新的，合计 ≤ 3×3=9 条主题记忆（在 top_k 基础上最多 +6）。

## 7. 可调参数

见 [06 · 工程骨架 §4](06-engineering.md) 的召回 / Topic 聚类分组。关键项：

| 键 | 默认 | 说明 |
|---|---|---|
| `cluster_min_size` | 10 | 全局积压触发门槛 |
| `topic_min_cluster` | 2 | 每簇够格线（≥此值才送 LLM） |
| `cluster_cos_floor` | 0.78 | 预聚类余弦门槛（不同 embedding 模型需重标定） |
| `topic_max_members` | 10 | 单 topic 最大记忆条数 |
| `cluster_scan_window` | 200 | 预聚类比较窗口 |
| `topic_expand_hits` / `topic_expand_cap` | 3 / 3 | 召回扩展：前 N 条命中的 topic / 每主题总量封顶 |
| `review_trigger_min` | 3 | 待复核触发阈值 |
| `consolidate_llm_enabled` | true | 启用 LLM 定稿（关则仅预聚类） |

**写死常量**（不暴露）：`CONSOLIDATE_BATCH_MAX=20`、`QUEUE_TASK_MAX_FAILS=3`。
