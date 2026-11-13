"""Clustering constants + embedding cosine helper.

There is NO keyword dictionary here anymore — the legacy deterministic topic
labeller has been removed. ``topic`` is produced only by LLM finalization
(see ``Engine.finalize_cluster``); ``cluster_id`` groups candidate members during
pre-clustering / direct clustering. This module holds just the algorithm-internal
constants (not exposed as config) and a cosine helper.
"""

from __future__ import annotations

from .vec import cosine_lists, unpack_vec

# ---- clustering states (stored in memories.cluster_stats) ----------------
UNCLUSTERED = "unclustered"        # 未聚类：cluster_id 为空
PRE_CLUSTERED = "pre_clustered"    # 预聚类：cluster_id 已生成，topic 仍空（候选中）
CLUSTERED = "clustered"            # 已聚类：topic 已由 LLM 定稿

# ---- algorithm-internal constants (tune by editing + tests, NOT config) --
PRE_CLUSTER_COS_FLOOR = 0.78       # single-link cosine floor: 宁滥勿缺（同主题尽量并，宁可多聚）
PRE_CLUSTER_SCAN_WINDOW = 200      # only compare against this many most-recent memories
TOPIC_MIN_CLUSTER = 2              # 每簇够格线：成员≥2 才算"一件事"、才可送 LLM 定稿（单条无需聚合）
CONSOLIDATE_BATCH_MAX = 20         # 单轮最多定稿簇数 = LLM 调用上限（有界）
CONSOLIDATE_LLM_ENABLED = True     # default ON: idle runner finalizes eligible clusters
QUEUE_TASK_MAX_FAILS = 3           # consecutive LLM failures before a cluster is abandoned
CHAT_BATCH_MAX_FAILS = 5           # consecutive extraction failures before a chat batch is given up
REVIEW_TRIGGER_MIN = 3             # inactive(待复核)条数 > 此值 → 触发闲时 review 任务
REVIEW_BATCH_MAX = 20              # 单轮最多处理多少条待复核记忆
LLM_TIMEOUT_S = 30
LLM_MAX_TOKENS = 512


def cosine(a: bytes | None, b: bytes | None) -> float:
    """Cosine between two float32 embedding blobs (0.0 if either missing)."""
    if not a or not b:
        return 0.0
    return cosine_lists(unpack_vec(a), unpack_vec(b))
