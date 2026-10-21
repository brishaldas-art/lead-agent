from agent.step1_input import build_queries

qs = build_queries("Sponsor", "Bengaluru", ["tech"], "Future Circuits")
for q in qs:
    print(q)

banned = ["sponsor", "college", "fest", "delegation", "student"]
for q in qs:
    assert not any(b in q.lower() for b in banned), q
assert any("Bengaluru" in q for q in qs)
assert any("Future Circuits" in q for q in qs)

print("query tests passed")