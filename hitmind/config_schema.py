"""HitMind memory-provider config surface for the Hermes Desktop config panel.

Hermes loads this by path (never as a package) from the provider directory
(``~/.hermes/plugins/hitmind/config_schema.py``). The web route
``GET/PUT /api/memory/providers/hitmind/config`` renders these fields and, with
``storage=flat_json``, reads/writes them to ``$HERMES_HOME/hitmind/config.json`` — so
the provider needs no read/write code. The adapter reads that same file as the
highest-precedence value source (then ``HITMIND_*`` env, then built-in default).

Fields are grouped into five sections via ``group``: 记忆抽取 / 召回 / Topic 聚类 /
生命周期管理 / 模型配置. This module imports ONLY ``plugins.memory.config_schema``.
"""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SECRET,
    KIND_SELECT,
    KIND_TEXT,
    STORAGE_FLAT_JSON,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)

G_EXTRACT = "记忆抽取"
G_RECALL = "召回"
G_IMPORTANCE = "重要性"
G_TOPIC = "Topic 聚类"
G_LIFE = "生命周期管理"
G_MODEL = "模型配置"

CONFIG_SCHEMA = ProviderConfigSchema(
    name="hitmind",
    label="HitMind 长期记忆",
    storage=STORAGE_FLAT_JSON,
    docs_url="",
    fields=(
        # ================= 记忆抽取 =================
        ProviderField(key="sync_roles", label="自动捕获角色", kind=KIND_SELECT, default="user", group=G_EXTRACT,
                      description="哪些说话角色会被自动捕获为记忆。",
                      options=(ProviderFieldOption("user", "仅 user"),
                               ProviderFieldOption("user,assistant", "user + assistant"))),
        ProviderField(key="sync_user_min", label="user 消息最小长度（字符）", kind=KIND_NUMBER, default="5",
                      group=G_EXTRACT, description="短于此长度的 user 消息不捕获。"),
        ProviderField(key="sync_assistant_min", label="assistant 消息最小长度（字符）", kind=KIND_NUMBER, default="10",
                      group=G_EXTRACT, description="短于此长度的 assistant 消息不捕获。"),
        ProviderField(key="consolidate_every_turns", label="chat 批次轮数阈值", kind=KIND_NUMBER, default="10",
                      group=G_EXTRACT, description="未处理消息数 ≥ 2×此值 时标记待处理批次（闲时抽取）。"),
        ProviderField(key="memory_max_chars", label="单条记忆最大长度（字符）", kind=KIND_NUMBER, default="1024",
                      group=G_EXTRACT, description="每条记忆内容的最大字符数；该限制会动态写入 LLM 提示词，提示超出时拆分为多条记忆（超出硬上限仍会截断；0=不限）。"),
        ProviderField(key="time_normalize", label="时间归一化（相对日期→绝对日期）", kind=KIND_BOOL, default="true",
                      group=G_EXTRACT, description="写入时把“昨天/下周三”等按逻辑时间补成绝对日期（原文保留，另存 normalized_text 参与检索并随结果返回）；确定性、零 LLM。"),
        ProviderField(key="tz_hours", label="时间戳时区（UTC 偏移小时）", kind=KIND_NUMBER, default="8",
                      group=G_EXTRACT, description="写入记忆时间戳所用的本地时区偏移（单数字，默认 8 即东八区/上海）。时间戳为秒级、T 分隔并带该偏移，如 2026-11-12T15:22:48+08:00。"),
        ProviderField(key="skip_contexts", label="跳过捕获的上下文", kind=KIND_TEXT,
                      default="cron,subagent,flush,background,skill_loop", group=G_EXTRACT,
                      description="逗号分隔；这些上下文中的消息不会被自动捕获。"),
        ProviderField(key="work_platforms", label="work 模式平台", kind=KIND_TEXT,
                      default="desktop,cli,tui,local", group=G_EXTRACT,
                      description="逗号分隔；这些平台按 work 模式（压缩驱动）处理，其余按 chat 模式。"),
        ProviderField(key="chat_cooldown_seconds", label="失败冷却（秒）", kind=KIND_NUMBER, default="1200",
                      group=G_EXTRACT, description="chat 批次连续抽取失败后冷却多久再重试（保留消息，绝不丢）。"),
        ProviderField(key="idle_consolidate_seconds", label="闲时阈值（秒）", kind=KIND_NUMBER, default="300",
                      group=G_EXTRACT, description="会话空闲 ≥ 此秒数才处理后台任务；0 关闭闲时整理。"),
        ProviderField(key="idle_poll_seconds", label="后台轮询间隔（秒）", kind=KIND_NUMBER, default="60",
                      group=G_EXTRACT, description="闲时后台线程每隔多少秒检查一次是否有待办（最小 15）。"),
        # ================= 召回 =================
        ProviderField(key="recall_top_k", label="召回条数（top_k）", kind=KIND_NUMBER, default="5",
                      group=G_RECALL, description="每轮注入/查询返回的最大命中条数。"),
        ProviderField(key="dense_floor", label="向量最低相似度（余弦）", kind=KIND_NUMBER, default="0.5",
                      group=G_RECALL, description="dense 检索的余弦门槛：低于此值的候选在融合前被丢弃（绝对余弦，非相对分）。不同 embedding 模型分布不同，需据此调整；0.5≈embeddinggemma 无关文本底噪。"),
        ProviderField(key="rerank_enabled", label="启用重排（cross-encoder）", kind=KIND_BOOL, default="true",
                      group=G_RECALL, description="对召回短名单用本地 rerank 模型重排后再取 top_k。模型不可用时自动跳过（fail-open）。"),
        ProviderField(key="rerank_model", label="重排模型 id", kind=KIND_TEXT,
                      default="temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8", group=G_RECALL,
                      description="本地 /rerank 端点（与 embedding 同一 base_url）使用的 cross-encoder 模型 id。"),
        ProviderField(key="rerank_pool", label="重排候选池（条）", kind=KIND_NUMBER, default="30",
                      group=G_RECALL, description="取前多少条一阶段候选交给 rerank 重排（越大越准越慢）。"),
        ProviderField(key="topic_expand_cap", label="同主题扩展：每主题总共保留条数", kind=KIND_NUMBER, default="3",
                      group=G_RECALL, description="每个被展开的 topic 最多保留的总条数（已命中的那条也算在内，最新优先）；去重后不补足。"),
        ProviderField(key="topic_expand_hits", label="同主题扩展：取前 N 条命中的 topic", kind=KIND_NUMBER, default="3",
                      group=G_RECALL, description="固定对 RRF 融合结果前 N 条记忆所在的 topic 做扩展（无论这几条是否同主题）。"),
        ProviderField(key="rel_weight", label="相关性权重（0–1）", kind=KIND_NUMBER, default="0.8",
                      group=G_RECALL, description="最终得分 = 相关性权重×相关度 + 重要性权重×重要性 + (余下)×新鲜度。"),
        ProviderField(key="importance_weight", label="重要性权重（0–1）", kind=KIND_NUMBER, default="0.1",
                      group=G_RECALL, description="最终得分中重要性的权重（默认 0.8/0.1/0.1 = 相关度/重要性/新鲜度）。"),
        # ================= 重要性（各 kind 默认重要性 0–1）=================
        ProviderField(key="importance_persona", label="persona 重要性（0–1）", kind=KIND_NUMBER, default="1.0",
                      group=G_IMPORTANCE, description="persona 类记忆写入时的默认重要性。"),
        ProviderField(key="importance_fact", label="fact 重要性（0–1）", kind=KIND_NUMBER, default="1.0",
                      group=G_IMPORTANCE, description="fact 类记忆写入时的默认重要性。"),
        ProviderField(key="importance_event", label="event 重要性（0–1）", kind=KIND_NUMBER, default="1.0",
                      group=G_IMPORTANCE, description="event 类记忆写入时的默认重要性。"),
        ProviderField(key="importance_procedure", label="procedure 重要性（0–1）", kind=KIND_NUMBER, default="1.0",
                      group=G_IMPORTANCE, description="procedure 类记忆写入时的默认重要性。"),
        # ================= Topic 聚类 =================
        ProviderField(key="topic_min_cluster", label="每簇够格线（条）", kind=KIND_NUMBER, default="2",
                      inline=True, group=G_TOPIC, description="一个簇成员 ≥ 此值才送 LLM 判定是否同一件事（单条不聚合）。"),
        ProviderField(key="cluster_min_size", label="触发积压门槛（条）", kind=KIND_NUMBER, default="10",
                      inline=True, group=G_TOPIC,
                      description="全部待整理(pre_clustered)成员数合计 ≥ 此值才启动一轮 topic 整理。"),
        ProviderField(key="cluster_cos_floor", label="聚类余弦门槛（0–1）", kind=KIND_NUMBER, default="0.78",
                      group=G_TOPIC,
                      description="两条记忆并入同簇的最小余弦相似度。不同 embedding 模型的相似度分布不同，需据此调整（不同模型维度/训练目标不同，门槛差异较大）。"),
        ProviderField(key="topic_max_members", label="单 topic 最大记忆条数", kind=KIND_NUMBER, default="10",
                      group=G_TOPIC, description="一个 topic 簇最多容纳的成员数，超过则另起新簇。"),
        ProviderField(key="cluster_scan_window", label="预聚类比较窗口（条）", kind=KIND_NUMBER, default="200",
                      group=G_TOPIC, description="新记忆只与最近 N 条已有记忆比较以决定归簇。"),
        # ================= 生命周期管理 =================
        ProviderField(key="episode_ttl_days", label="episode 保留天数", kind=KIND_NUMBER, default="90",
                      group=G_LIFE, description="压缩摘要 episode 保留天数，到期物理删除。"),
        ProviderField(key="raw_ttl_days", label="原始捕获保留天数", kind=KIND_NUMBER, default="30",
                      group=G_LIFE, description="未整理的逐轮原始记忆保留天数（0=不清理）。"),
        ProviderField(key="capacity_warn", label="容量告警阈值（条）", kind=KIND_NUMBER, default="200000",
                      group=G_LIFE, description="active 记忆超过此值仅告警，不删除。"),
        ProviderField(key="version_keep", label="记忆保留历史版本数", kind=KIND_NUMBER, default="5",
                      group=G_LIFE, description="一条记忆被多次更新时，保留多少条旧版本（更早的标记删除）。"),
        ProviderField(key="review_trigger_min", label="待复核触发阈值（条）", kind=KIND_NUMBER, default="3",
                      group=G_LIFE, description="被判为待复核(inactive)的记忆超过此条数，闲时用 LLM 判定是否同一件事并更新版本。"),
        ProviderField(key="ttl_event_days", label="event 过期天数", kind=KIND_NUMBER, default="90",
                      group=G_LIFE, description="event 类记忆的默认过期天数。"),
        ProviderField(key="ttl_persona_days", label="persona 过期天数（0=永不过期）", kind=KIND_NUMBER, default="0",
                      group=G_LIFE, description="persona 类默认过期天数；0 表示不按时间过期。"),
        ProviderField(key="ttl_fact_days", label="fact 过期天数（0=永不过期）", kind=KIND_NUMBER, default="0",
                      group=G_LIFE, description="fact 类默认过期天数；0 表示不按时间过期。"),
        ProviderField(key="ttl_procedure_days", label="procedure 过期天数（0=永不过期）", kind=KIND_NUMBER, default="0",
                      group=G_LIFE, description="procedure 类默认过期天数；0 表示不按时间过期。"),
        ProviderField(key="half_life_event_days", label="event 半衰期（天）", kind=KIND_NUMBER, default="14",
                      group=G_LIFE, description="event 类新鲜度半衰期。"),
        ProviderField(key="half_life_persona_days", label="persona 半衰期（天）", kind=KIND_NUMBER, default="180",
                      group=G_LIFE, description="persona 类新鲜度半衰期。"),
        ProviderField(key="half_life_fact_days", label="fact 半衰期（天）", kind=KIND_NUMBER, default="180",
                      group=G_LIFE, description="fact 类新鲜度半衰期。"),
        ProviderField(key="half_life_procedure_days", label="procedure 半衰期（天）", kind=KIND_NUMBER, default="60",
                      group=G_LIFE, description="procedure 类新鲜度半衰期。"),
        # ================= 模型配置 =================
        ProviderField(key="llm_timeout_s", label="LLM 调用超时（秒）", kind=KIND_NUMBER, default="90",
                      group=G_MODEL, description="单次宿主 LLM 调用的硬超时，防止挂死。"),
        ProviderField(key="consolidate_llm_enabled", label="启用 LLM 定稿", kind=KIND_BOOL, default="true",
                      group=G_MODEL, description="关闭则只做零 LLM 预聚类，不做 topic 定稿（省 LLM）。"),
        ProviderField(key="bg_runner", label="启用闲时后台线程", kind=KIND_BOOL, default="true",
                      group=G_MODEL, description="关闭后 chat 批次与 topic 整理不会自动运行。"),
        ProviderField(key="embedding_base_url", label="Embedding 服务地址（/v1）", kind=KIND_TEXT, default="",
                      group=G_MODEL, description="如 http://127.0.0.1:8091/v1；留空则退化为纯 BM25。"),
        ProviderField(key="embedding_model", label="Embedding 模型名", kind=KIND_TEXT,
                      default="embeddinggemma-2", group=G_MODEL, description="本地 embedding 服务的模型名。"),
        ProviderField(key="embedding_api_key", label="Embedding API Key", kind=KIND_SECRET, default="",
                      env_key="HITMIND_EMBED_API_KEY", group=G_MODEL,
                      description="本地服务通常留空；保存在 ~/.hermes/.env，不写入 config.json。"),
    ),
)
