# dsh-himind 基于hitmind-core的适配DeepSeek Harness的原生插件

> 这是一个主打本地优先、轻量、可被多个 Agent 共同读写的长期记忆服务。

HitMind 支持 work 和 chat 两种模式，分别适用于长对话与短对话。底层基于检索增强生成（RAG），可独立配置向量模型与Rerank模型。

## 核心特性

- **多 Agent 共享**：共享靠 Core + Schema + 同一个 SQLite 文件，不靠某个接口类。有钩子的宿主走 Provider + 压缩钩子；无钩子的宿主走 MCP。
- **轻量、core 零第三方依赖**：python纯标准库，单文件 SQLite，**无重型服务与知识图谱**，作为hermes插件安装时**按需披露**工具。
- **合理利用上下文压缩机制**：利用hermes压缩后钩子，episode 直接保存宿主累积压缩摘要，由 LLM 做「上次 vs 这次」的差异比较、增量抽取记忆；
- **闲时可扩展整理**：写时零 LLM 预聚类（余弦单链）→ 闲时拉取式 LLM 定稿 topic / 复核版本 / chat 批次抽取；任务可插拔、默认开启。
- **低LLM调用**：仅部分闲时任务会调用LLM，不阻塞正常AGENT流程。
- **混合检索 + 二级精排**：Dense + BM25 + RRF 融合排序+rerank精排。

## 安装（DSH）

DSH 插件以 npm 包形式分发，`dsh plugin` 命令内部转发 pnpm 安装：

```bash
dsh plugin --profile <name> add dsh-hitmind
```

插件本体（JS adapter）只负责工具注册、注入、捕获与配置；真正的记忆引擎是 HitMind 的 Python core，以自带二进制（`hitmind-daemon`）的 stdio sidecar 形式运行。**用户无需安装 Python**：adapter 通过 `optionalDependencies` 声明了各平台的 runtime 包，pnpm / npm 会依据当前操作系统的 `os` / `cpu` 字段**只安装匹配当前平台的那一个**，其余平台自动跳过。

运行时由插件内部自动定位已安装的 runtime 二进制（`resolveBundledDaemon()`），不需要任何"数据库路径 / Python 解释器 / 二进制路径"之类的配置。

**数据位置**：记忆库默认落在 DSH 的数据目录 `$DSH_HOME/hitmind/data/hitmind.db`（`$DSH_HOME` 默认 `~/.dsh`），可用环境变量 `HITMIND_DB` 覆盖。该库为敏感数据，目录与库文件均按仅所有者可访问（`0700` / `0600`）创建。

## 发布（维护者）

发布分为**两个独立的 npm 包**：

| 包 | 内容 | 发布时机 |
|---|---|---|
| `dsh-hitmind` | JS adapter（`index.js` / `client.js` / `cordis.patch.yml` / `package.json` / `README.md`） | 每次版本更新 |
| `dsh-hitmind-runtime-<os>-<arch>` | 单个自包含 `hitmind-daemon` 二进制（PyInstaller onefile，约 12MB） | 每个目标平台各发一次 |

`package.json` 的 `optionalDependencies` 列出了全部支持的平台组合；未发布（404）的平台组合会被 pnpm / npm 视作"不可选"而自动忽略，因此可以分批发布。

### 自动构建发布（推荐，GitHub Actions）

PyInstaller 无法交叉编译，每个平台必须在对应 OS/arch 上原生构建。`.github/workflows/runtime.yml` 用 runner 矩阵一次性构建并发布全部平台：

| runtime 包 | runner |
|---|---|
| `dsh-hitmind-runtime-linux-x64` | `ubuntu-latest` |
| `dsh-hitmind-runtime-linux-arm64` | `ubuntu-24.04-arm` |
| `dsh-hitmind-runtime-darwin-arm64` | `macos-latest` |
| `dsh-hitmind-runtime-darwin-x64` | `macos-15-intel` |
| `dsh-hitmind-runtime-win32-x64` | `windows-latest` |

- **PR / 手动（publish=false）**：只构建并把各平台 tgz 作为 artifact 上传（矩阵冒烟，不发布）。
- **打 tag `v*` 或手动 publish=true**：构建并 `npm publish` 全部 runtime 包。
- 鉴权：推荐 **npm Trusted Publishing（OIDC）**（`id-token: write` 已在 workflow 声明，无需长期 token；需在 npmjs 为每个包配置 trusted publisher，npm≥11.5.1 / Node≥22.14）；或用仓库 secret `NPM_TOKEN`。macOS 构建会 `codesign --sign -` 做 ad-hoc 签名。
- runtime 版本号由 `build-runtime.sh` 从 `pyproject.toml` 读取（单一来源），adapter 版本需手动同步到同一版本。

### 本地单平台构建（调试用）

```bash
cd dsh-plugin/runtime
./build-runtime.sh                 # 构建并打包本机平台到 dist/（不发布）
./build-runtime.sh --publish       # 额外 npm publish（需 npm 登录）
# 平台标签可用 HITMIND_RUNTIME_OS / HITMIND_RUNTIME_ARCH 覆盖（CI 用）
# 解释器默认锁定 CPython 3.12（HITMIND_BUILD_PYTHON 可改），产物约 12MB
```

> 版本号：adapter 与各 runtime 包应保持相同的 `version`；runtime 来自 `pyproject.toml`，adapter 发版时同步。
