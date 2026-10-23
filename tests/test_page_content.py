from agent.discovery import _page_from_fetch
from agent.page_content import extract_footer_text


def test_extracts_visible_footer_text_from_common_footer_patterns():
    html = """
    <html><body>
      <footer>Registered office: Kolkata. <a href="mailto:info@example.in">info@example.in</a>
        <span hidden>Hidden note: Mumbai</span>
        <div aria-hidden="true"><span><b>Nested hidden descendant</b></span></div>
        <span style="display: none">Hidden email: hidden@example.in</span>
        <script>const city = 'Fake City';</script>
      </footer>
      <div id="site-footer">Support office: Patna</div>
      <div role="contentinfo">Copyright 2026</div>
    </body></html>
    """

    text = extract_footer_text(html)

    assert "Registered office: Kolkata" in text
    assert "info@example.in" in text
    assert "Support office: Patna" in text
    assert "Copyright 2026" in text
    assert "Hidden note" not in text
    assert "hidden@example.in" not in text
    assert "Fake City" not in text


def test_page_from_fetch_uses_same_site_rendered_footer(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_RENDER", "1")
    monkeypatch.setattr(
        "agent.discovery.render_page",
        lambda _url: {
            "status_code": 200,
            "final_url": "https://www.example.in/",
            "html": "<html><body><footer>Office Kolkata; info@example.in</footer></body></html>",
            "text": "Office Kolkata; info@example.in",
        },
    )

    page = _page_from_fetch({
        "status": "ok",
        "final_url": "https://example.in/",
        "text": "Static homepage",
        "html": "<html><body><main>Static homepage</main></body></html>",
    })

    assert page["render_status"] == "ok"
    assert page["render_url"] == "https://www.example.in/"
    assert "Office Kolkata" in page["footer_text"]
    assert "info@example.in" in page["rendered_text"]


def test_page_from_fetch_rejects_renderer_redirect_to_another_domain(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_RENDER", "1")
    monkeypatch.setattr(
        "agent.discovery.render_page",
        lambda _url: {
            "status_code": 200,
            "final_url": "https://unrelated.example/",
            "html": "<footer>Kolkata info@unrelated.example</footer>",
            "text": "Kolkata info@unrelated.example",
        },
    )

    page = _page_from_fetch({
        "status": "ok",
        "final_url": "https://example.in/",
        "text": "Static homepage",
        "html": "<html><body><main>Static homepage</main></body></html>",
    })

    assert page["render_status"] == "rejected_redirect_or_failure"
    assert page["rendered_html"] == ""
    assert "unrelated" not in page["footer_text"]


def test_every_fetched_homepage_page_record_carries_extracted_footer():
    pages = [
        _page_from_fetch({
            "status": "ok",
            "final_url": "https://one.example/",
            "text": "Homepage one",
            "html": "<html><footer>Office: Kolkata</footer></html>",
        }),
        _page_from_fetch({
            "status": "ok",
            "final_url": "https://two.example/",
            "text": "Homepage two",
            "html": "<html><div class='site-footer'>Office: Patna</div></html>",
        }),
    ]

    assert [page["footer_text"] for page in pages] == [
        "Office: Kolkata",
        "Office: Patna",
    ]
