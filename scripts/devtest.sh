#!/usr/bin/env bash
# devtest.sh — HitMind 改动后的"不重启网关"一键验证
#
#   ./scripts/devtest.sh            # pytest + 一次 hermes -z 冒烟 + 生产库 diff
#   ./scripts/devtest.sh --no-zh    # 跳过 hermes -z（只跑 pytest + 直连插件）
#
# 原理：
#   - plugins.isolation=in_process 且加载器命中 sys.modules 就复用 → 网关进程内的代码
#     只在启动时导入一次，改磁盘代码必须重启网关才生效。
#   - 但 `hermes -z` 是"全新一次性进程"，会从磁盘重新发现/加载插件，跑完整
#     register→initialize→sync_turn→prefetch 生命周期 → 用它即可不重启网关地验证新代码。
#   - 直连插件（阶段 B）用网关同款解释器，秒级验证 core/适配器逻辑。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
DB="$HOME/.hermes/hitmind/data/hitmind.db"
GW_PY="$(ls "$HOME"/.hermes/tools/python-3.14.9*/bin/python3 2>/dev/null | head -1)"
GW_SP="$HOME/.hermes/hermes-agent/.venv/lib/python3.14/site-packages"
DO_ONESHOT=1
[ "${1:-}" = "--no-zh" ] && DO_ONESHOT=0

fail() { echo "✗ $*" >&2; exit 1; }
dbcount() { python3 -c "import sqlite3,os;c=sqlite3.connect('file:'+os.path.expanduser('~/.hermes/hitmind/data/hitmind.db')+'?mode=ro',uri=True);print(c.execute('SELECT COUNT(*) FROM memories').fetchone()[0])" 2>/dev/null || echo "?"; }

echo "=========== A. pytest（core + 适配器逻辑）==========="
uv run pytest -q || fail "pytest 未通过"

echo
echo "=========== B. 直连插件（秒级，网关同款解释器）==========="
if [ -z "$GW_PY" ]; then
  echo "⚠ 未找到网关 Python，跳过 B/C"
else
  PYTHONPATH="$GW_SP:$HOME/.hermes/hermes-agent" "$GW_PY" - <<'PY' || fail "直连插件冒烟失败"
import sys, os, tempfile, json
os.environ.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))
from plugins.memory import load_memory_provider
p = load_memory_provider("hitmind")
assert p and p.is_available(), "provider 不可用"
p.initialize("devtest", hermes_home=tempfile.mkdtemp(), platform="local", agent_context="primary")
# layered model: sync_turn no longer writes; exercise episode + remember + recall directly
ep = p._engine.add_episode("devtest 冒烟会话摘要", session_id="devtest")["data"]["id"]
p._engine.remember("devtest 冒烟：数据库选 SQLite，召回走 BM25+Dense。", "fact", episode_id=ep)
r = json.loads(p.handle_tool_call("hitmind_recall", {"query": "SQLite 召回", "top_k": 3}))
assert r.get("ok") and r["data"]["results"], "recall 无结果"
assert any(x.get("episode_summary") for x in r["data"]["results"]), "episode 上下文未附带"
p.shutdown()
print("  直连插件 OK：episode+remember+recall+episode上下文 通过")
PY
fi

echo
echo "=========== C. hermes -z 一次性进程（真机壳，新代码）==========="
if [ "$DO_ONESHOT" = "1" ]; then
  before="$(dbcount)"
  echo "  生产库条目(前): $before"
  timeout 240 hermes -z "devtest 冒烟：只回复 OK" 2>&1 | tail -3
  after="$(dbcount)"
  echo "  生产库条目(后): $after  (分层模型下 oneshot 为 work 模式、不逐轮写 memories，条目数通常不变)"
  [ "$after" = "?" ] || [ "$after" -ge "$before" ] || fail "hermes -z 库条目异常（减少了）"
  # 自清理：把本条 devtest 探针软删（按内容前缀 + 近 5 分钟），不污染真实记忆
  python3 - <<'PY'
import sqlite3, os, datetime
p = os.path.expanduser("~/.hermes/hitmind/data/hitmind.db")
c = sqlite3.connect(p); cur = c.cursor()
cut = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
rows = cur.execute(
    "SELECT id FROM memories WHERE status='active' AND content LIKE 'devtest 冒烟%' AND created_at >= ?",
    (cut,)).fetchall()
for (mid,) in rows:
    cur.execute("UPDATE memories SET status='deleted' WHERE id=?", (mid,))
c.commit()
print(f"  已清理 devtest 探针 {len(rows)} 条")
c.close()
PY
else
  echo "  (--no-zh：跳过)"
fi

echo
echo "✅ devtest 完成。改代码后跑本脚本即可，无需重启网关。"
echo "   注意：仅'网关常驻态'相关改动（长连接/多轮缓存/极端并发）仍需重启验证。"
