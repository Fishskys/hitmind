from hitmind.core import tokenize


def test_cjk_bigram_hits_two_char_word():
    toks = tokenize.tokenize("记忆插件")
    assert "记忆" in toks and "插件" in toks


def test_cjk_unigram_present():
    assert "记" in tokenize.tokenize("记忆")


def test_latin_words_and_entities_kept():
    toks = tokenize.tokenize("see https://x.io/a and foo_bar on 2026-11-09")
    assert "foo_bar" in toks
    assert "https://x.io/a" in toks
    assert "2026-11-09" in toks


def test_search_text_space_joined():
    st = tokenize.search_text("记忆插件 memory")
    assert " " in st and "记忆" in st.split()
