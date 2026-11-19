# Changelog

本文件记录 HitMind 的版本变更，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。条目按版本倒序排列，每类变更按 Added / Changed / Deprecated / Removed / Fixed / Security 归类。

## [Unreleased]

## [0.2.0] - 2026-11-19

DSH（DeepSeek Harness）原生插件首次发布（npm），并把配置与工具定义收敛为单一事实源。

### Added
- **DeepSeek Harness 原生插件**：`dsh-hitmind`（JS adapter，`defineTool` 11 工具 + 每轮自动注入 + 会话/压缩捕获 + 浏览器配置卡），Python core 以自包含 `hitmind-daemon` stdio sidecar 运行——用户无需安装 Python。
- **多平台 runtime 分包**：`dsh-hitmind-runtime-<os>-<arch>`（linux-x64/arm64、darwin-arm64/x64、win32-x64），adapter 经 `optionalDependencies` + `os`/`cpu` 自动选装；GitHub Actions runner 矩阵原生构建并**以 Trusted Publishing（OIDC）+ provenance 发布**到 npm。
- `hitmind/daemon.py`：宿主无关的 stdio JSON-RPC sidecar（含 `DaemonTurnStore` 会话/压缩去重、`ingest_compaction` 幂等全量抽取）。
- `hitmind/settings_spec.py`：配置**单一事实源**（56 项），同时驱动 Hermes `config_schema`、Hermes 适配器与 DSH daemon 的引擎参数，杜绝默认值漂移。
- `hitmind/tools.py`：11 个工具 schema + dispatch 的**单一事实源**，Hermes、MCP、DSH 三端一致。
- `hitmind/capture_policy.py`：捕获决策唯一实现（`should_capture`），Hermes 与 DSH 共用。
- DSH 插件页本地化元数据（`locale/en.json`、`locale/zh.json`）。
- README：MCP 服务章节（启动/自检/客户端配置片段）、开源协议小节。

### Changed
- 配置与工具定义从各端各自维护改为单一事实源生成（行为不变，消除漂移）。
- 移除 Hermes 适配器中重复的工具/配置定义，改由 `settings_spec` / `tools` 驱动。

### Fixed
- 记忆库文件权限收紧为**仅所有者可访问**（目录 `0700`、库文件 `0600`，`core/db.py`）。
- DSH daemon 默认库路径父目录自动创建（多层嵌套亦可）。
- 跨平台构建：Windows venv 解释器（`Scripts/python.exe`）识别、锁定 CPython 3.12（产物 ~12MB）、macOS ad-hoc 签名。

## [0.1.2] - 2026-11-14

### Added
- 工具改为**渐进式披露（按需加载）**：全部工具通过宿主 registry 以 `toolset="hitmind"` 注册，默认不常驻提示词，由宿主的 `tool_search` / `tool_describe` / `tool_call` 桥按需检索与加载（Hermes）。常驻工具面近乎为零；未提供 `register_tool` 的宿主自动回退到原 `get_tool_schemas` 暴露。

### Changed
- 工具集统一为 **11 个**：`hitmind_export` / `hitmind_import` 并入主工具集，与其余 9 个核心工具同一套注册与派发；**MCP 与 Hermes 插件工具集保持一致**（9 核心 + export/import）。

### Removed
- 移除 `HITMIND_EXPOSE_IMPORT_EXPORT` 环境变量（改为按需加载后，export/import 不再需要单独开关）。

## [0.1.1] - 2026-11-14

### Changed
- 记忆 ID 由 30 字符的 `mem_<26>` 缩短为 `m_` + 8 位 base32（约 40 bit，共 10 字符）；episode ID 同步缩短。
- 召回注入格式统一为 `[id] (kind)[@创建日期]：正文`（此前为 `- [id] (kind) 标题` 标题卡）；正文取原文，归一文本不再注入作答。
- 时间戳改为秒级精度、`T` 分隔、带本地时区偏移，如 `2026-11-12T15:22:48+08:00`（此前为微秒级 UTC）。
- 时间归一化：裸「周X / 星期X」统一按本周（周一为周首）解析，消除同一句「周一」在不同基准日算出两个日期的问题。
- `normalized_text` 明确定位为仅检索索引：参与向量 / FTS 索引、使绝对日期可检索，不再进入作答上下文。

### Added
- 新增配置 `tz_hours`：默认 `auto` 读取系统时区（零配置），也可手填 `8` / `5.5` / `5:30` 等，支持半点与刻钟时区（`+05:30`、`+05:45`）。
- 新增文档 `docs/architecture/07-benchmark-experiments.md` 的当前成绩：chat-memory-benchmark 1.0 六人物 714 题，开 rerank 660/714（92.4%）、无 rerank 631/714（88.4%）。

### Fixed
- 记忆 ID 主键碰撞不再可能导致覆盖或写入失败：冲突时自动换号重试（最多 8 次），原记忆始终不受影响。

## [0.1.0] - 2026-11-13

首个版本：本地优先、轻量、可被多个 Agent 共同读写的长期记忆服务。

### Added
- 宿主无关 Core：纯 Python + 单文件 SQLite，core 零第三方依赖；多个 Agent 共享同一份记忆（单一真相），有钩子的宿主走原生绑定，无钩子的宿主走 MCP。
- 写入与整理：写时确定性判定（零 LLM）完成去重 / 冲突 / 版本化，拿不准的进入复核队列（拒写不丢文本）；由压缩摘要驱动增量抽取、chat 游标批次抽取，以及可插拔的闲时 LLM 整理。
- 分层记忆：episode（上下文锚点）+ memory（可检索记忆单元）两层，原文不作为记忆体系的一层。
- 检索：Dense + BM25 混合 + RRF 融合，可配向量余弦门槛；二级 cross-encoder 精排（可降级）；同主题召回扩展；写入时相对日期归一化。
- 生命周期：按 kind 差异化衰减与 TTL，重要性与新鲜度参与排序；容量只告警不删，记忆不硬删、只归档。
- 接口与配置：Hermes 原生绑定 + MCP（stdio）；桌面配置页全量参数化；JSON 记忆导出 / 导入。
- 工程保障：外部调用（LLM / embedding / rerank）超时与连续失败熔断、自动降级；提交前敏感信息检查（pre-commit）。

[Unreleased]: https://github.com/Fishskys/hitmind/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Fishskys/hitmind/compare/v0.1.3...v0.2.0
[0.1.2]: https://github.com/Fishskys/hitmind/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Fishskys/hitmind/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Fishskys/hitmind/releases/tag/v0.1.0
