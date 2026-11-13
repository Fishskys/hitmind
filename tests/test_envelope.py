from hitmind.adapters.hermes.envelope import sanitize_user_content


def test_plain_text_passes():
    assert sanitize_user_content("我明天三点后不喝咖啡") == "我明天三点后不喝咖啡"


def test_pure_machine_notice_dropped():
    assert sanitize_user_content("[IMPORTANT: Background process job completed]") is None
    assert sanitize_user_content("[System note: The user switched models]") is None


def test_envelope_stripped_keeps_human_tail():
    s = "[System note: you resumed] 帮我看下昨天那个冻干的事"
    out = sanitize_user_content(s)
    assert out == "帮我看下昨天那个冻干的事"


def test_compaction_summary_keeps_user_message():
    s = "summary stuff --- END OF CONTEXT SUMMARY --- respond to the message below --- 你好，继续吧"
    out = sanitize_user_content(s)
    assert out and "你好，继续吧" in out
