# HitMind

> 本地优先、轻量、可被多个 Agent 共同读写的长期记忆服务。Core 是一套宿主无关的纯 Python + SQLite 引擎。

HitMind 从日常对话中识别值得长期保留的信息，整理成可复用的记忆单元，按需准确召回，并维护新旧关系与规模。它把「写时确定性判定（零 LLM）」与「闲时可选 LLM 整理」分开：默认零模型也能跑，聪明是增量。

完整设计与决策见 [`docs/architecture/`](docs/architecture/README.md)（`docs/framework.md` 为指向该索引的薄指针）。

---

## 核心特性

- **多 Agent 共享**：共享靠 Core + Schema + 同一个 SQLite 文件，不靠某个接口类。有钩子的宿主走 Provider + 压缩钩子；无钩子的宿主走 MCP。
- **轻量、core 零第三方依赖**：纯标准库，单文件 SQLite，无重型服务与知识图谱。作为 Provider 只向AGENT暴露 **9 个工具（schema ≈660 tokens）**，无编译扩展、无需 side venv，装进宿主 Python 即可跑。
- **合理利用上下文压缩机制**：利用hermes压缩后钩子，episode 直接保存宿主累积压缩摘要，由 LLM 做「上次 vs 这次」的差异比较、增量抽取记忆；
- **闲时可扩展整理**：写时零 LLM 预聚类（余弦单链）→ 闲时拉取式 LLM 定稿 topic / 复核版本 / chat 批次抽取；任务可插拔、默认开启。
- **低LLM调用**：仅部分闲时任务会调用LLM，不阻塞正常AGENT流程。
- **混合检索 + 二级精排**：Dense + BM25 + RRF 融合排序+rerank精排。

---

## 系统双层记忆架构

HitMind 的记忆分两层：**L1 episode** 承接上下文，**L2 memory** 是最终可检索的记忆单元。**原文（宿主的完整转录）不属于记忆体系**，仅作为外部、可选的来源。

```
原文/转录   宿主 state.db（messages）—— 外部、可选的"尽力而为"来源，非 HitMind 的一层
              │  work：压缩时只读取"累积压缩摘要"；chat：按游标读取"未处理轮次"
              ▼
L1 episode  一个 session 一条；work 存压缩摘要，chat 存窗口批次(range)
              │  抽取记忆
              ▼
L2 memory   persona / fact / event / procedure，episode_id 指回 L1
```

两条捕获路径，都在**闲时**才调 LLM：

- **work 模式（压缩驱动）**：宿主压缩时，把累积压缩摘要存为 episode，LLM 比较「这次 vs 上次」只抽增量记忆（摘要累积有损，避免整体重抽重复）。
- **chat 模式（游标批次）**：热路径只记活动、打「待处理」标记；闲时按游标取未处理轮次，一次 LLM 抽取后**成功才推进游标**，失败保留游标与消息重试（绝不丢消息）。

这样 L1 提供「当时在聊什么」的上下文锚点，L2 提供可独立检索/向量化的记忆单元，兼顾上下文完整与检索精度，且写路径始终零 LLM。详见 [08 · 分层记忆](docs/architecture/08-layered-capture.md)。

---

## 架构一览

```
        ┌───────────── 记忆 Core（宿主无关，单一真源） ─────────────┐
        │  schema · 存储(SQLite) · 检索 · 关系/版本 · 生命周期        │
        │  · 事务/并发 · scope · provenance                          │
        └───────────────────────────────────────────────────────────┘
              ▲                                         ▲
   ┌──────────┴──────────┐                  ┌───────────┴─────────────┐
   │ 有钩子的宿主          │                  │ 无钩子的宿主（走 MCP）     │
   │ 原生 Provider         │                  │ 其他 Agent                │
   │ (Provider + 压缩钩子) │                  │ (MCP: stdio / HTTP)       │
   └──────────────────────┘                  └───────────────────────────┘
```

- **Core**（`hitmind/core/`）：`Engine` 门面 + 写闸门 + 检索 + 生命周期 + 预聚类 + 闲时任务，零 Host 依赖。
- **宿主绑定**（`hitmind/adapters/hermes/`）：Provider + 钩子（`sync_turn` 只记活动、压缩时 `ingest_compaction`）+ 后台轮询线程 + 桌面配置。
- **MCP server**（`hitmind/mcp/`）：供嵌不了 Python 的宿主，token 名 = `agent_id`。

详见 [00 · 总览](docs/architecture/00-overview.md) / [01 · 数据模型](docs/architecture/01-core-data-model.md) / [02 · 接口契约](docs/architecture/02-api-contract.md) / [08 · 分层记忆](docs/architecture/08-layered-capture.md)。

---

## 快速上手

### 环境依赖

- Python **>= 3.11**（core 无需任何编译扩展）。
- [uv](https://docs.astral.sh/uv/)（推荐用于建 venv 与依赖管理）。
- 可选：本地 embedding 服务（OpenAI 兼容 `/v1/embeddings`，同机可带 `/rerank`）。**缺失时自动降级为 BM25-only、跳过 rerank，不会挂**。

### 安装与测试

```bash
git clone <repo> hitmind && cd hitmind
uv sync --extra dev
uv run pytest          # 全部单测（unit + 契约 + 并发 + 端到端切片）
uv run ruff check      # lint
```

### 安装到 Hermes（作为记忆 Provider）

HitMind 可以作为 Hermes 的 **记忆 Provider 插件**：

#### 常规安装

```bash
# 1) 取得包目录（源码仓库根下的 hitmind/ 即插件目录）
git clone <repo> hitmind-src && cd hitmind-src

# 2) 复制到 Hermes 用户插件目录（目录内含 plugin.yaml + __init__.py(register)）
cp -r hitmind "$HERMES_HOME/plugins/hitmind"

# 3) 指定记忆 Provider，检查并重启网关生效
hermes config set memory.provider hitmind
hermes memory status        # Provider=hitmind / installed ✓ / available ✓
```

- 数据库默认位于 `$HERMES_HOME/hitmind/data/hitmind.db`（自动创建）。
- **多 Hermes home / 多套环境**：对每个 home 各复制一份到其 `plugins/hitmind`（各自独立代码目录，互不共享）。

#### 提供的工具（9 个）

作为记忆 Provider，HitMind 向模型暴露 **9 个工具**（`capture` / `consolidate` 为内部/派发用、不暴露；其余配置项不占工具预算）：

| 工具 | 作用 |
|---|---|
| `hitmind_remember` | 写入一条记忆（走写闸门：去重 / 冲突 / 版本化）|
| `hitmind_recall` | 混合检索（Dense + BM25 + RRF，可选二级精排），返回 top_k |
| `hitmind_get` | 按 id 取正文（`history=true` 取版本链）|
| `hitmind_update` | 版本化更新（不覆盖，保留历史版本）|
| `hitmind_forget` | 软删除 / 归档 |
| `hitmind_list` | 分页浏览 / 过滤 |
| `hitmind_reviews` | 列出待复核记忆（inactive）|
| `hitmind_review_resolve` | 裁决待复核（accept / reject / supersede）|
| `hitmind_stats` | 计数 / 健康 / embedding 状态 |

> **轻量**：仅 **9 个工具，schema 约 2.3K 字符（≈660 tokens）**，对上下文与首 token 延迟更友好；另外提供`export` / `import` 工具，按需使用。

### 独立使用（MCP）

```bash
uv sync --extra mcp
uv run hitmind-mcp            # stdio；streamable-HTTP 规划中
```

---

## 配置说明

**优先用桌面配置页**：Plugins → HitMind 设置，分组呈现 40+ 项。解析优先级：**config.json（桌面表单）→ `HITMIND_*` 环境变量 → 内置默认**；解析在适配层完成，Core 只认构造参数。

常见可调项（部分）：

| 分组 | 配置 | 默认 | 作用 |
| --- | --- | --- | --- |
| 记忆抽取 | `memory_max_chars` | 1024 | 单条记忆正文长度上限（动态写入提示词，超出提示拆分）|
| 记忆抽取 | `time_normalize` | true | 写入时相对日期归一（相对→绝对）|
| 召回 | `recall_top_k` / `dense_floor` | 5 / 0.5 | 返回条数 / dense 余弦门槛 |
| 召回 | `rerank_enabled` / `rerank_model` / `rerank_pool` | true / mmarco-mMiniLMv2 / 30 | 二级 cross-encoder 重排（开关/模型/候选池）|
| 召回 | `rel_weight` / `importance_weight` | 0.8 / 0.1 | 最终分权重（余下给新鲜度）|
| 重要性 | `importance_{persona,fact,event,procedure}` | 1.0 | 各 kind 写入时的默认重要性 |
| Topic 聚类 | `cluster_cos_floor` / `topic_min_cluster` | 0.78 / 2 | 预聚类相似度门槛 / 每簇够格线 |
| Topic 聚类 | `cluster_min_size` / `topic_max_members` | 10 / 10 | 全局积压门槛 / 单簇上限 |
| 生命周期 | `version_keep` / `episode_ttl_days` | 5 / 90 | 版本保留份数 / episode TTL |
| 生命周期 | `idle_consolidate_seconds` | 300 | 闲时整理轮询间隔（0=关闭）|

密钥（如 embedding / rerank 的 API key）只放宿主密钥文件，不进 `config.json`、不回显。完整清单见 [`hitmind/config_schema.py`](hitmind/config_schema.py) 与 [06 · 工程骨架](docs/architecture/06-engineering.md)。

---

## 路线图

- [x] **v1 核心**：Core（SQLite / 写闸门 / 检索 / 生命周期）+ 宿主绑定 + MCP，core 零依赖。
- [x] **压缩驱动 episode + 增量抽取**；chat 游标批次；可扩展闲时线程。
- [x] **topic 拉取式双阈值定稿**（写时零 LLM 预聚类 → 闲时 LLM 定稿，含 `corrections` 落库）。
- [x] **全量参数化桌面配置页 + 记忆重要性参与打分 + JSON 导出/导入**。
- [x] **混合检索增强**：dense 余弦门槛可配、二级 cross-encoder 重排、写入时时间归一化。
- [ ] 真实语料上的检索/聚类阈值校准；`episode_summary` 在召回标题卡中的呈现。

---

## 参与贡献

欢迎 Issue 与 Pull Request。改动请附带测试，并保持 `uv run pytest` 与 `uv run ruff check` 全绿；架构相关决策写入 `docs/architecture/` 对应模块（不写回单一文档）。

---

## 致谢

HitMind 的部分设计思路参考了以下记忆插件 / 项目，特此致谢：

- **mnemosyne** — 记忆分层、写路径与后台整理的取舍
- **dsh-memu-memory** — 确定性写闸门、冲突检测与闲时策展
- **astrbot_plugin_livingmemory** — 固定轮数整理的游标策略
- **chat-memory-benchmark** — 端到端长期记忆评测基准

---

## 文档索引

- 架构总览与分模块文档：[`docs/architecture/README.md`](docs/architecture/README.md)
- 设计决策薄指针：[`docs/framework.md`](docs/framework.md)

