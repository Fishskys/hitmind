# dsh-himind 基于hitmind-core的适配DeepSeek Harness的原生插件

> 这是一个主打本地优先、轻量、可被多个 Agent 共同读写的长期记忆服务。

HitMind 是一款适配 hermes 和 deepseek harness 及其它多种 Agent 的共享记忆系统，支持 work 和 chat 两种模式，分别适用于长对话与短对话。底层基于检索增强生成（RAG），可独立配置向量模型与Rerank模型。该系统目前提供 hermes 和 dsh 的原生记忆插件和通用 MCP 服务。

## 核心特性

- **多 Agent 共享**：多Agent之间可共用同一份记忆库，共享同一份记忆
- **轻量、core 零第三方依赖**：python纯标准库，单文件 SQLite，**无重型服务与知识图谱**，作为hermes插件安装时**按需披露**工具。
- **合理利用上下文压缩机制**：利用hermes压缩后钩子，episode 直接保存宿主累积压缩摘要，由 LLM 做「上次 vs 这次」的差异比较、增量抽取记忆；
- **闲时可扩展整理**：写时零 LLM 预聚类（余弦单链）→ 闲时拉取式 LLM 定稿 topic / 复核版本 / chat 批次抽取；任务可插拔、默认开启。
- **低LLM调用**：仅部分闲时任务会调用LLM，不阻塞正常AGENT流程。
- **混合检索 + 二级精排**：Dense + BM25 + RRF 融合排序+rerank精排。

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

## 提供的工具

11 个工具的 schema 合计约 3.0K 字符（≈870 tokens）

| 工具                     | 作用                                                     |
| ------------------------ | -------------------------------------------------------- |
| `hitmind_remember`       | 写入一条记忆（走写闸门：去重 / 冲突 / 版本化）           |
| `hitmind_recall`         | 混合检索（Dense + BM25 + RRF，可选二级精排），返回 top_k |
| `hitmind_get`            | 按 id 取正文（`history=true` 取版本链）                  |
| `hitmind_update`         | 版本化更新（不覆盖，保留历史版本）                       |
| `hitmind_forget`         | 软删除 / 归档                                            |
| `hitmind_list`           | 分页浏览 / 过滤                                          |
| `hitmind_reviews`        | 列出待复核记忆（inactive）                               |
| `hitmind_review_resolve` | 裁决待复核（accept / reject / supersede）                |
| `hitmind_stats`          | 计数 / 健康 / embedding 状态                             |
| `hitmind_export`         | 导出记忆为 JSON（备份）                                  |
| `hitmind_import`         | 从 JSON 导入记忆（校验，merge / replace）                |



## **官方链接**

**Github：**https://github.com/Fishskys/hitmind

**npmjs：**https://www.npmjs.com/package/dsh-hitmind

