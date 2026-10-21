import fitz

LINES = [
    "TechFest 2026 - Bengaluru Institute of Technology",
    "Theme: Future Circuits",
    "Expected audience: 2000 students",
    "Venue: Bengaluru, Karnataka",
    "",
    "About the Fest",
    "Three days of hackathons, robotics, and talks on emerging technology.",
    "",
    "Our Past Sponsors",
    "Acme Systems",
    "Nova Foods",
    "Brightline Telecom",
    "",
    "Participating Colleges",
    "Sri Vidya Engineering College",
    "Greenfield Institute of Technology",
]

doc = fitz.open()
page = doc.new_page()
y = 60
for line in LINES:
    page.insert_text((50, y), line, fontsize=12)
    y += 22
doc.save("data/sample_brochure.pdf")
print("saved data/sample_brochure.pdf")
