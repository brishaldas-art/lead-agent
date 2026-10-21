from agent.replan import should_replan, validate_queries

# Re-plan decision
assert should_replan(5, 15, 0, 2) is True
assert should_replan(20, 15, 0, 2) is False
assert should_replan(5, 15, 2, 2) is False

# Query validation
used = ["acme systems sponsors bengaluru"]
candidates = [
    "college fest sponsors bengaluru",
    "https://evil.com/x",
    "Acme Systems Sponsors Bengaluru",
    "ab",
    "tech companies csr head bengaluru",
    "x y z w v u t s r",
    "bengaluru engineering college tech fest",
    "fifth new query bengaluru sponsors",
]
out = validate_queries(candidates, used)
print(out)

expected = [
    "college fest sponsors bengaluru",
    "tech companies csr head bengaluru",
    "bengaluru engineering college tech fest",
    "fifth new query bengaluru sponsors",
]
assert out == expected, out
print("all replan tests passed")