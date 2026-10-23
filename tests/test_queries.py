from agent.step1_input import build_queries

qs = build_queries("Sponsor", "Bengaluru", ["tech"], "Future Circuits")
for q in qs:
    print(q)

banned = ["sponsor", "college", "fest", "delegation", "student"]
for q in qs:
    assert not any(b in q.lower() for b in banned), q
assert any("Bengaluru" in q for q in qs)
assert any("Future Circuits" in q for q in qs)

renewable_queries = build_queries("Sponsor", "Kolkata", ["renewable energy"], None)
assert renewable_queries
assert all("renewable energy" in q.casefold() for q in renewable_queries), renewable_queries
assert all("Kolkata" in q for q in renewable_queries), renewable_queries
assert not any("IT services" in q or "tech company careers" in q for q in renewable_queries)

digital_queries = build_queries("Sponsor", "Kolkata", ["software", "IT services"], None)
assert len(digital_queries) == 10, digital_queries
assert all("Kolkata" in q for q in digital_queries), digital_queries
assert any("software" in q.casefold() for q in digital_queries)
assert any("IT services" in q for q in digital_queries)
assert not any("manufacturers" in q or "CSR" in q for q in digital_queries)

it_services_queries = build_queries("Sponsor", "Kolkata", ["IT services"], None)
assert len(it_services_queries) == 10, it_services_queries
assert "IT companies Kolkata" in it_services_queries
assert "software development firms Kolkata" in it_services_queries
assert "IT consulting firms Kolkata" in it_services_queries
assert all(len(q.split()) <= 5 for q in it_services_queries)

print("query tests passed")