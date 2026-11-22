# 06 · 工程骨架

> 模块文档 · HitMind 架构文档集（索引见 [README](README.md)）。

**范围**：目录结构、依赖、运行时形态、配置面、外部调用失败保险

## 1. 目录结构

```
hitmind/                          # 仓库根（docs/ references/ AGENTS.md README.md …）
├── pyproject.toml                # uv 管理；包元数据/依赖/入口
├── uv.lock
├── hitmind/                      # 扁平包布局（包目录直接在根）
│   ├── __init__.py               # 导出 Engine, __version__
│   ├── config_schema.py          # 桌面配置页字段（分组：记忆抽取/召回/重要性/Topic聚类/生命周期/模型/日志）
│   ├── logging_setup.py          # 统一日志：格式/级别/脱敏 + 宿主接入 + 独立进程本地落盘
│   ├── io_cli.py                 # 记忆 JSON 导出/导入 CLI（export/import，--dry-run 校验）
│   ├── core/                     # 宿主无关引擎（零 Host 依赖）
│   │   ├── engine.py             # Engine：11 动词门面 + 召回/闲时任务编排
│   │   ├── db.py                 # 连接/WAL/BEGIN IMMEDIATE/迁移入口
│   │   ├── schema.py             # DDL（memories / memories_fts / episodes / sessions / topic_tasks / meta）
│   │   ├── models.py             # dataclass: Memory/HitMindError/错误码
│   │   ├── gate.py               # 写闸门：dedup/conflict/版本化（确定性，零 LLM）
│   │   ├── tokenize.py           # CJK uni/bi/tri n-gram + latin 实体保护
│   │   ├── retrieval.py          # BM25(FTS5)+dense+RRF+阈值
│   │   ├── vec.py                # float32 打包 + 余弦（纯标准库）
│   │   ├── timenorm.py           # 写入时相对日期归一（纯标准库）
│   │   ├── embedder.py           # Embedder/Reranker 协议 + HTTP 客户端 + Null 降级
│   │   ├── lifecycle.py          # TTL/half_life/capacity 告警/过期判定
│   │   ├── query_rewrite.py      # 查询改写（多路召回，确定性）
│   │   └── topic.py              # 预聚类/定稿常量
│   ├── adapters/hermes/          # Hermes 绑定（Provider + 钩子 + 后台线程 + LLM 适配）
│   └── mcp/server.py             # MCP server（stdio）
├── bench/                        # 检索微基准（corpus/metrics/run）
├── tests/                        # pytest（unit + 契约 + 并发）
└── scripts/                      # acceptance_smoke.py / hitmind_io.py / devtest.sh 等
```

**边界规则**：`core/` 不 import 任何 `adapters/`、`mcp/` 或宿主符号；adapters 只依赖 `core` 的公开 API；`bench/`、`tests/` 可依赖 core，不被 core 依赖。

## 2. 依赖与打包

- `requires-python=">=3.11"`、扁平包布局 + hatchling/uv 构建后端。
- **core 依赖：无第三方依赖（纯标准库）**。向量只需 `array`+`math` 打包与余弦（`core/vec.py`），无 numpy，故可在任意 CPython 3.11+ 直接 `import` core，无需编译扩展。
- **optional**：`mcp = ["mcp"]`（只有 MCP server 需要）。dev：`pytest`、`ruff`。
- **入口**：`hitmind-mcp = "hitmind.mcp.server:main"`、`hitmind-io = "hitmind.io_cli:main"`。
- **部署到宿主**：包根 `hitmind/` 本身就是插件目录（其 `__init__.py` 暴露 `register(ctx)`），用**软链**到宿主的 `plugins/hitmind` 即可，不用复制。

## 3. 运行时形态

Core 是嵌入式 Python 库，可被宿主进程直接 import。按宿主能力有三种形态：

| 形态 | 用宿主 Python？ | 能力 | 适用 |
|---|---|---|---|
| **① 原生 Provider（默认）** | 是（import 进宿主进程） | 全量：压缩钩子、每轮自动 `prefetch`、宿主 LLM | 能 import Python 且有钩子的宿主 |
| **② side-venv 包装** | 是，但依赖装进独立 side venv | 同①，且宿主重建 venv 不冲掉依赖 | 依赖是按 CPython 版本绑定的编译扩展、且宿主会重建环境 |
| **③ MCP 独立进程** | 否（独立解释器/进程，任意 Python 3.11+） | 仅工具调用：无压缩钩子、无自动注入、无宿主 LLM | 嵌不了 Python 的宿主 |

- **为什么 HitMind 不需要 ②**：core 零第三方依赖，无编译扩展可冲突、无版本锁定需求，直接软链进宿主插件目录用宿主 Python 跑即可。
- **外部能力都走网络/宿主接口，不是 Python 包依赖**：embedding/rerank → HTTP 调本地服务（缺失降级）；LLM → 宿主提供的接口（不直连、不存 key）。这正是"core 零依赖、又能在宿主环境干活"的关键。
- **要跑在完全独立的 Python 上**：改走 **③ MCP 进程**，代价是失去压缩钩子/自动召回/宿主 LLM。

## 4. 配置面

- 所有可调参数由 `hitmind/config_schema.py` 声明（`ProviderConfigSchema` + `ProviderField`，分组展示），`storage=flat_json` → 读写 `$HERMES_HOME/hitmind/config.json`。
- 解析优先级：**config.json（桌面表单）→ `HITMIND_*` 环境变量 → 内置默认**；解析职责全在适配层 `initialize()`，**Core 只认构造参数**、不读环境变量。
- 仅 secret（如 embedding/rerank API key）走宿主密钥文件，不进 config.json、不回显。

**召回与嵌入相关配置项（节选；完整见 `config_schema.py`）**：

| 分组 | 键 | 默认 | 作用 |
|---|---|---|---|
| 召回 | `recall_top_k` | 5 | 每轮注入/查询返回的最大命中条数 |
| 召回 | `dense_floor` | 0.5 | dense 检索余弦门槛（绝对余弦，非相对分） |
| 召回 | `rerank_enabled` / `rerank_model` / `rerank_pool` | true / 本地 cross-encoder / 30 | 二级精排（开关/模型/候选池） |
| 召回 | `topic_expand_hits` / `topic_expand_cap` | 3 / 3 | 同主题扩展：前 N 条命中的 topic / 每主题总量封顶 |
| 召回 | `rel_weight` / `importance_weight` | 0.8 / 0.1 | 最终分权重（余下给新鲜度）|
| 记忆抽取 | `memory_max_chars` | 1024 | 单条记忆最大字符数（动态写入提示词，超出提示拆分）|
| 记忆抽取 | `time_normalize` | true | 写入时相对日期归一开关 |

## 5. 外部调用失败保险

原则：**任何外部调用都不能无休止占用/重试**；写路径保持零 LLM；外部不可用时一律降级而非阻塞。

| 调用 | 保险 |
|---|---|
| **宿主 LLM**（finalize / extract / review）| 尝试次数有界（≤2 次，不循环）；每次尝试硬超时（`llm_timeout_s`，默认 90s），超时即放弃该次（防宿主 hang 卡死闲时 pass）|
| **topic 定稿** | 失败计数达 `QUEUE_TASK_MAX_FAILS=3` 后 `dropped`（之后须簇增长才再试）；每轮 `CONSOLIDATE_BATCH_MAX=20` 上限；无 LLM → 不动 |
| **chat 批次抽取** | 连续失败达上限 → 进入冷却 `chat_cooldown_seconds`（默认 1200s）：**保留游标与消息（绝不推进游标、绝不丢消息）**、清零重试计数；冷却结束再重试同一窗口 |
| **episode 抽取** | 单次调用；失败返回 `llm_failed`，不自动重试（下次压缩是新事件）|
| **HTTP embedding / rerank** | 单次 `timeout`；**连续失败熔断**（置 `ok=False`）后短路后续调用；召回自动降级 BM25 / 跳过 rerank；idle runner 每轮 `reset()` 探测恢复 |

## 6. 日志与可观测性

统一实现见 `hitmind/logging_setup.py`（零第三方依赖，纯标准库 `logging`）。**输出位置随宿主形态而定，格式统一**：

- **优先接入宿主日志系统**：Hermes 适配器在 `register()` 里调用 `use_host_logging()` —— 只设 `hitmind` logger 的级别并 `propagate=True`，让记录**经宿主既有的根 handler**（如 `agent.log`）输出、沿用宿主格式；**不改根 logger 级别、不往根或 `hitmind` 挂 handler、不写自有文件**（零全局副作用）。
- **独立进程（MCP server / stdio daemon）本地落盘**：`configure()` 装一个 `RotatingFileHandler`（默认 `$HITMIND_HOME/hitmind/logs/hitmind.log`，5MB×5 份，可由 `log_file`/`HITMIND_LOG_FILE` 改），**从不写 stdout**（stdout 是 MCP/JSONL 协议通道）；daemon 另镜像到 stderr 供宿主插件逐行采集。

**统一格式**（`HitMindFormatter`，毫秒精度）：

```
2026-11-22 16:30:00.123 [hitmind-core] [INFO] 内容
<时间>                 [<组件>]     [<级别>] <消息>
```

组件标签取值：`hitmind-core`（引擎/存储）、`hitmind-hermes`（Hermes 适配）、`hitmind-mcp`（MCP server）、`hitmind-dsh`（stdio daemon）。级别 `DEBUG/INFO/WARNING/ERROR/CRITICAL`，由 `log_level`/`HITMIND_LOG_LEVEL` 控制（默认 INFO）。

**记录范围（INFO 只记必要事件）**：
- 关键写：`remember`/`capture` 的 action/id/version/status（DEBUG）、容量告警（WARNING）。
- 关键读：`recall` 的 top_k/命中数（DEBUG）。
- **后台任务（重点）**：`chat_batch` 每轮汇总与成功提交、`topic`/`review` 每轮处理/定稿/拒绝/失败计数、`ingest_compaction` 的 revision/首压/抽取条数、idle runner 启停 —— 均为 INFO 且**仅在确有任务时输出**，空转轮只发 DEBUG，避免刷屏。
- 错误：各 fail-open 兜底 `WARNING`、未捕获异常 `ERROR`（含栈）。

**隐私红线（硬规则）**：绝不写记忆正文、原始对话、Episode 摘要、LLM Prompt、API Key/密钥；日志只记结构/跳过原因/计数/错误。`sanitize()` 仅作最后兜底（截断超长消息 + 对 Bearer/api_key/sk-/URL 内凭据做 `[REDACTED]`），**不能替代"根本不传入敏感值"的纪律**。

### 6.1 排障指南（Troubleshooting）

**开 DEBUG**：大量决策日志是 DEBUG 级（默认 INFO 看不到）。临时排障时设 `HITMIND_LOG_LEVEL=DEBUG`（或 `hitmind/config.json` 的 `log_level`）后重启对应进程/Hermes 网关即可；排查完记得调回 INFO。

**"为什么某个会话一直没产出记忆？"——按链路看这几行日志**：

| 症状 | 先看这行日志 | 说明 |
|---|---|---|
| 消息根本没被捕获 | `sync_turn skip capture sid= role=user len= user_min=`（DEBUG） | 被 capture_policy 挡掉（多为字数门槛）。改 `sync_user_min`（默认 0=不限）。|
| 没到触发线 | `note_activity sid= cursor= unprocessed= threshold= pending=`（DEBUG） | 看未处理回合数 vs 阈值(=2×`consolidate_every_turns`)。|
| 到了线但没置位 | `note_activity` 无输出 + `chat batch due`（INFO）缺失 | state_source 不可用或无会话（`note_activity skip`）。|
| 置位了但没抽 | `chat_batch skip sid= reason=empty/no_llm/llm_failed/cooldown`（INFO/WARNING） | 逐条原因：empty=读不到消息；no_llm=宿主 LLM 不可用；llm_failed=抽取返回非法；cooldown=连续失败冷却。|
| 抽取在读时异常 | `state.db read failed (...)`（WARNING） | state.db 读取异常（如连接/线程/权限问题）。|
| 跑了哪版/什么配置 | 启动行 `hitmind ready version= ... consolidate_every_turns= ... sync_user_min= ...`（INFO） | 一眼确认版本与关键配置，排除"跑了旧代码"。|

后台 idle runner 每轮若有待处理会话，会打一行 `chat_batch pass sessions= committed= deferred= failed= cooling=`（INFO）；`deferred` 表示会话仍活跃（未空闲满 `idle_consolidate_seconds`，默认 300s）而顺延到下一空闲窗口。

