from agent.discovery import _page_from_fetch
from agent.page_content import extract_footer_text


def test_extracts_visible_footer_text_from_common_footer_patterns():
    html = """
    <html><body>
      <footer>Registered office: Kolkata. <a href="mailto:info@example.in">info@example.in</a>
        <span hidden>Hidden note: Mumbai</span>
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
