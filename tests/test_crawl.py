from agent.crawl import score_links

html = (
    '<a href="/team">Our Team</a>'
    '<a href="/about-us">About</a>'
    '<a href="/contact">Contact</a>'
    '<a href="/sponsors">Sponsors</a>'
    '<a href="/careers">Careers</a>'
    '<a href="/brochure.pdf">Brochure</a>'
    '<a href="https://linkedin.com/company/x">LinkedIn</a>'
)

for result in score_links(html, "https://acmesystems.in/"):
    print(result)