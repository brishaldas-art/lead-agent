"""City aliases are consistent across discovery and export gates."""

from agent.location import city_variants, text_mentions_city


def test_common_city_aliases_match():
    assert city_variants("Kolkata") == ("Kolkata", "Calcutta")
    assert text_mentions_city("Our office is in Calcutta, West Bengal.", "Kolkata")
    assert text_mentions_city("Bengaluru office", "Bangalore")
    assert not text_mentions_city("We serve the Kolkata region remotely", "Mumbai")


if __name__ == "__main__":
    test_common_city_aliases_match()
    print("City alias tests passed.")
