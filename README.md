# HitMind

> 这是一个主打本地优先、轻量、可被多个 Agent 共同读写的长期记忆服务。

HitMind 是一款适配 hermes 和 deepseek harness 及其它多种 Agent 的共享记忆系统，支持 work 和 chat 两种模式，分别适用于长对话与短对话。底层基于检索增强生成（RAG），可独立配置向量模型与Rerank模型。该系统目前提供 hermes 和 dsh 的原生记忆插件和通用 MCP 服务。

完整设计与决策见 [`docs/architecture/`](docs/architecture/README.md)。

记忆测评：在[chat-memory-benchmark](https://github.com/Ornn8/chat-memory-benchmark) 1.0六人物测评集中成绩为660/714，92.4%（开启rerank），LLM调用成本仅1.8元。

---

## 核心特性

- **多 Agent 共享**：多Agent之间可共用同一份记忆库，共享同一份记忆
- **轻量、core 零第三方依赖**：python纯标准库，单文件 SQLite，**无重型服务与知识图谱**，作为hermes插件安装时**按需披露**工具。
- **合理利用上下文压缩机制**：利用hermes压缩后钩子，episode 直接保存宿主累积压缩摘要，由 LLM 做「上次 vs 这次」的差异比较、增量抽取记忆；
- **闲时可扩展整理**：写时零 LLM 预聚类（余弦单链）→ 闲时拉取式 LLM 定稿 topic / 复核版本 / chat 批次抽取；任务可插拔、默认开启。
- **低LLM调用**：仅部分闲时任务会调用LLM，不阻塞正常AGENT流程。
- **混合检索 + 二级精排**：Dense + BM25 + RRF 融合排序+rerank精排。

---

## 系统双层记忆架构

HitMind 的记忆分两层：**L1 episode** 承接上下文，**L2 memory** 是最终可检索的记忆单元。

```
原文/转录   宿主 state.db（messages）—— AGENT对话原文
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

- Python **>= 3.11**
- [uv](https://docs.astral.sh/uv/)
- 可选：embedding 、rerank服务。**未配置时自动降级为 BM25-only、跳过 rerank，但效果不佳**。

### 安装

```bash
git clone https://github.com/Fishskys/hitmind.git && cd hitmind
uv sync --extra dev
```

### 安装到 DeepSeek Harness （作为插件）

在dsh中添加插件，输入`dsh-hitmind`并安装即可

或

```bash
dsh plugin --profile <name> add dsh-hitmind
```

数据库默认位置：`$DSH_HOME/hitmind/data/hitmind.db`

### 安装到 Hermes（作为记忆 Provider）

HitMind 可以作为 Hermes 的 **记忆 Provider 插件**：

#### 常规安装

```bash
# 1) 取得包目录（源码仓库根下的 hitmind/ 即插件目录）
git clone https://github.com/Fishskys/hitmind.git && cd hitmind

# 2) 复制到 Hermes 用户插件目录（目录内含 plugin.yaml + __init__.py(register)）
cp -r hitmind "$HERMES_HOME/plugins/hitmind"

# 3) 指定记忆 Provider，检查并重启网关生效
hermes config set memory.provider hitmind
hermes memory status        # Provider=hitmind / installed ✓ / available ✓
```

- 数据库默认位于 `$HERMES_HOME/hitmind/data/hitmind.db`（自动创建）。

#### 提供的工具（11 个）

作为 hermes 插件使用时，HitMind 提供了 **11 个工具**，全部使用 hermes registry 注册，并按需加载。

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
| `hitmind_export` | 导出记忆为 JSON（备份）|
| `hitmind_import` | 从 JSON 导入记忆（校验，merge / replace）|

> **轻量**：11 个工具的 schema 合计约 3.0K 字符（≈870 tokens），但**全部按需加载、不常驻**——常规会话里这部分上下文开销几乎为零；用完即释放。

---

## MCP 服务（通用接入）

### 安装

在项目根目录运行：

```bash
uv sync --extra mcp
```

### 启动 / 自检

```bash
hitmind-mcp --agent-id my-agent          # stdio，供 MCP 客户端 spawn
hitmind-mcp --selftest                    # 不接客户端，跑一轮写入+召回后退出（验证安装）
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--db <path>` | `$HERMES_HOME/hitmind/data/hitmind.db`（缺省 `~/.hermes/...`） | 记忆库路径。指向同一文件即可与其它宿主/Agent **共享同一份记忆** |
| `--agent-id <id>` | `mcp`（或环境变量 `HITMIND_AGENT_ID`） | 该客户端的写入标识 |
| `--selftest` | — | 跑一轮 core 往返后退出，用于验证安装 |

**可选环境变量（建议配置）：**

- Embedding：`HITMIND_EMBED_BASE_URL`、`HITMIND_EMBED_MODEL`（默认 `embeddinggemma-2`）、`HITMIND_EMBED_API_KEY`
- Rerank：`HITMIND_RERANK_BASE_URL`、`HITMIND_RERANK_MODEL`、`HITMIND_RERANK_API_KEY`

### 客户端配置片段

```json
{
  "mcpServers": {
    "hitmind": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/hitmind", "hitmind-mcp", "--agent-id", "claude"],
      "env": {
        "HITMIND_EMBED_BASE_URL": "http://127.0.0.1:8091/v1",
        "HITMIND_EMBED_MODEL": "embeddinggemma-2"
      }
    }
  }
}
```

> 想多 Agent 共享同一份记忆：把 `--db` 指向同一个库文件即可

---

## 配置说明

在hermes desktop的设置-记忆与上下文页可以看到全部配置，以下仅展示常用配置：

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

完整清单见 [`hitmind/config_schema.py`](hitmind/config_schema.py) 与 [06 · 工程骨架](docs/architecture/06-engineering.md)。

---

## 路线图

- [x] Deepseek harness适配插件设计

---

## 参与贡献

欢迎 Issue 与 Pull Request。改动请附带测试，并保持 `uv run pytest` 与 `uv run ruff check` 全绿；架构相关决策写入 `docs/architecture/` 对应模块（不写回单一文档）。

---

## 开源协议

本项目采用 [MIT License](LICENSE) 发布。

Copyright (c) 2026 Fishskys

你可以自由使用、复制、修改、合并、发布、分发、再授权及销售本软件，但需在上述版权声明与许可声明随附的前提下。本软件按"原样"提供，不附带任何明示或默示的担保。

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
- 设计决策索引：[`docs/framework.md`](docs/framework.md)

