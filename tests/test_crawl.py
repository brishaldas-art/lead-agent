from agent.crawl import score_links


def test_contact_page_is_prioritized_within_small_crawl_cap():
    html = (
        '<a href="/team">Our Team</a>'
        '<a href="/about-us">About</a>'
        '<a href="/contact-us">Contact Us</a>'
        '<a href="/csr">CSR</a>'
        '<a href="/careers">Careers</a>'
        '<a href="/brochure.pdf">Brochure</a>'
        '<a href="https://linkedin.com/company/x">LinkedIn</a>'
    )

    results = score_links(html, "https://acmesystems.in/", max_links=2)

    assert results[0][0] == "https://acmesystems.in/contact-us"
    assert "https://acmesystems.in/team" in {url for url, _score in results}
    assert all("linkedin.com" not in url for url, _score in results)
    assert all("careers" not in url and not url.endswith(".pdf") for url, _score in results)
