from news_aggregator.fetcher.canonical import canonicalize_url, clean_text, normalize_title


def test_canonical_strips_tracking_and_normalises_host():
    a = canonicalize_url("http://www.Example.com/news/story/?utm_source=x&id=7&fbclid=abc#top")
    b = canonicalize_url("https://example.com/news/story?id=7")
    assert a == b == "https://example.com/news/story?id=7"


def test_canonical_keeps_meaningful_params_sorted():
    assert canonicalize_url("https://x.com/a?b=2&a=1") == "https://x.com/a?a=1&b=2"


def test_clean_text_removes_html_and_entities():
    assert clean_text("<p>Fed &amp; ECB <b>hold</b>\n rates</p>") == "Fed & ECB hold rates"


def test_normalize_title_drops_outlet_suffix_and_punctuation():
    assert normalize_title("Fed holds rates steady - Reuters") == "fed holds rates steady"
    assert normalize_title("Fed holds rates steady!") == "fed holds rates steady"
