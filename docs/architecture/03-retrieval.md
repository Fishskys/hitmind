# 03 · 检索栈

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：混合检索（Dense + BM25）、融合、阈值、二级 rerank、前缀与嵌入契约

## 1. 检索流程

```
query ─► [服务端前缀包装] ─┬─► Dense（embedding → 余弦）  ─┐
                          └─► BM25（FTS5，CJK n-gram）   ─┤
        ─► 每路带下限归一 → RRF 融合 → final_score → 二级 rerank → top_k
```

- 两路各取 `limit×3` 候选（引擎传 `limit=50` → 每路 ≤150），去重并集后融合；融合结果全量参与打分、排序，再交 rerank 重排，最后取 `top_k`。
- **阈值卡在可解释原始信号上**：dense 用**绝对余弦门槛 `dense_floor`**（默认 0.50，融合前丢弃低于它的候选），BM25 支持绝对/相对下限。**无相关记忆时返回空**。
- **可降级**：embedding 服务不可用 → 自动 BM25-only，不 fail。
- **主题扩展**（前 N 条命中 topic 的同主题追加）见 [05](05-topic-clustering.md)。

## 2. 融合与打分

- 默认 `fusion=rrf`（RRF，k=60）：`rel_norm = 融合分 / 本轮最高分`（相对名次分，第 1 名恒为 1.0）。
- 每一候选按 `final = rel_weight·rel_norm + importance_weight·importance + 余下·freshness` 排序（默认 `0.8/0.1/0.1`，见 [04](04-lifecycle.md)）。
- 归一化加权线性融合（`fusion=linear`）作为可选：`dense` 减 `dense_floor` 底噪后归一、`bm25` 归一，`rel = w_dense·ndn + w_bm25·nbm`。

## 3. 二级 rerank（cross-encoder）

在 `top_k` 截断前，用本地 cross-encoder 对短名单重排：

- **接口**：本地 `/rerank`（与 embedding 同 base_url，路由在服务根，非 `/v1` 下）。请求 `{model, query, documents, top_n?}`，返回 `results:[{index, relevance_score|score}]`（**原始 logits，可负、无界，仅用于排序，不设阈值**）。
- **候选池**：取一阶段前 `rerank_pool`（默认 30）条交 rerank，重排后再取 `top_k`。
- **fail-open**：无 reranker / 关闭 / 端点失败或返回空 → 保持一阶段顺序，recall 绝不因重排失败。
- **开关**：`rerank_enabled`（默认开）、`rerank_model`、`rerank_pool`（配置面，见 [06](06-engineering.md)）。

## 4. 嵌入前缀归属

前缀由**服务端**按每模型配置施加（`models.yaml`），客户端**只传 `input_type`**：`query` → `query_prefix`，`document` → `doc_prefix`（`doc_prefix` 可含 `{title}`，传 `title` 替换）。**客户端绝不自己再拼前缀**（双前缀 = 同一文本加两次、映射到错误子空间，检索静默变差）。同一模型只能有一处施加前缀。库记模型 id/维度，**换模型必须整库重嵌 + 启动维度校验**。

## 5. 分词与阈值经验

- 分词：CJK `uni/bi/tri` n-gram（零依赖，<3 字也命中），latin 词与日期等实体整体保留。
- 经验：余弦底噪约 0.5（无关中文文本也在此附近），故 `dense_floor` 默认 0.5；提高门槛可降噪但可能误伤"语义弱相关"难例（它们常靠 BM25/topic 兜底）。
- 融合 `RRF` 与归一化加权在小样本上差异不大，保留 RRF 为默认；`rel_ratio`（相对 top 剪枝）为可选。

> 衰减与综合分细节见 [04 · 生命周期](04-lifecycle.md)。
