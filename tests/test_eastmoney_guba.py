"""Tests for Eastmoney Guba community-post fetching."""

from __future__ import annotations


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body


def test_fetch_guba_posts_uses_default_discussion_list(monkeypatch):
    from tradingagents.dataflows import eastmoney_guba as guba

    seen_urls = []
    html = """
    <tr class="listitem">
      <div class="read">123</div>
      <div class="reply">4</div>
      <div class="title">
        <a data-postid="12345" href="/news,002559,12345.html">散户真实讨论标题</a>
      </div>
      <div class="author"><a href="/user">股友9920K0W283</a></div>
      <div class="update">07-06 08:31</div>
    </tr>
    """

    def fake_urlopen(req, timeout):
        seen_urls.append(req.full_url)
        return _FakeResponse(html)

    monkeypatch.setattr(guba, "urlopen", fake_urlopen)

    result = guba.fetch_guba_posts("002559.SZ", limit=1, trade_date="2026-07-06")

    assert seen_urls[0] == "https://guba.eastmoney.com/list,002559.html"
    assert "股友9920K0W283: 散户真实讨论标题" in result


def test_fetch_guba_posts_merges_discussion_and_media_streams(monkeypatch):
    from tradingagents.dataflows import eastmoney_guba as guba

    seen_urls = []
    discussion_html = """
    <tr class="listitem">
      <div class="read">123</div>
      <div class="reply">4</div>
      <div class="title">
        <a data-postid="12345" href="/news,002559,12345.html">散户真实讨论标题</a>
      </div>
      <div class="author"><a href="/user">股友9920K0W283</a></div>
      <div class="update">07-06 08:31</div>
    </tr>
    """
    media_html = """
    <tr class="listitem">
      <div class="read">5678</div>
      <div class="reply">90</div>
      <div class="title">
        <a data-postid="67890" href="/news,002559,67890.html">龙虎榜数据解读</a>
      </div>
      <div class="author"><a href="/user">亚威股份资讯</a></div>
      <div class="update">07-06 07:08</div>
    </tr>
    """

    def fake_urlopen(req, timeout):
        seen_urls.append(req.full_url)
        if req.full_url.endswith(",1,f.html"):
            return _FakeResponse(media_html)
        return _FakeResponse(discussion_html)

    monkeypatch.setattr(guba, "urlopen", fake_urlopen)

    result = guba.fetch_guba_posts("002559.SZ", limit=4, trade_date="2026-07-06")

    assert seen_urls == [
        "https://guba.eastmoney.com/list,002559.html",
        "https://guba.eastmoney.com/list,002559,1,f.html",
    ]
    assert "散户/社区讨论" not in result
    assert "资讯/媒体流" not in result
    assert "股友9920K0W283: 散户真实讨论标题" in result
    assert "亚威股份资讯: 龙虎榜数据解读" in result


def test_fetch_guba_posts_deduplicates_posts_across_streams(monkeypatch):
    from tradingagents.dataflows import eastmoney_guba as guba

    duplicate_html = """
    <tr class="listitem">
      <div class="read">100</div>
      <div class="reply">1</div>
      <div class="title">
        <a data-postid="11111" href="/news,600276,11111.html">重复资讯标题</a>
      </div>
      <div class="author"><a href="/user">恒瑞医药资讯</a></div>
      <div class="update">07-06 07:27</div>
    </tr>
    """

    def fake_urlopen(req, timeout):
        return _FakeResponse(duplicate_html)

    monkeypatch.setattr(guba, "urlopen", fake_urlopen)

    result = guba.fetch_guba_posts("600276.SS", limit=3, trade_date="2026-07-06")

    assert result.count("恒瑞医药资讯: 重复资讯标题") == 1


def test_fetch_guba_posts_does_not_filter_media_authors_from_discussion(monkeypatch):
    from tradingagents.dataflows import eastmoney_guba as guba

    discussion_html = """
    <tr class="listitem">
      <div class="read">100</div>
      <div class="reply">1</div>
      <div class="title">
        <a data-postid="11111" href="/news,600276,11111.html">普通列表里的资讯标题</a>
      </div>
      <div class="author"><a href="/user">恒瑞医药资讯</a></div>
      <div class="update">07-06 07:27</div>
    </tr>
    """

    def fake_urlopen(req, timeout):
        if req.full_url.endswith(",1,f.html"):
            return _FakeResponse("")
        return _FakeResponse(discussion_html)

    monkeypatch.setattr(guba, "urlopen", fake_urlopen)

    result = guba.fetch_guba_posts("600276.SS", limit=3, trade_date="2026-07-06")

    assert "恒瑞医药资讯: 普通列表里的资讯标题" in result
