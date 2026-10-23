"""City-name normalization shared by discovery and final export checks."""

from __future__ import annotations

import re

# Common Indian city aliases. Keep canonical names and aliases together so a
# requested city can match the way an official site spells the same location.
_CITY_GROUPS = (
    ("Bengaluru", "Bangalore"),
    ("Kolkata", "Calcutta"),
    ("Chennai", "Madras"),
    ("Mumbai", "Bombay"),
    ("Puducherry", "Pondicherry"),
    ("Thiruvananthapuram", "Trivandrum"),
    ("Prayagraj", "Allahabad"),
    ("Vadodara", "Baroda"),
    ("Mangaluru", "Mangalore"),
    ("Mysuru", "Mysore"),
)

_ALIAS_TO_GROUP = {
    alias.casefold(): group
    for group in _CITY_GROUPS
    for alias in group
}


def city_variants(city: str | None) -> tuple[str, ...]:
    """Return the requested city plus known aliases, ordered and deduplicated."""
    value = " ".join(str(city or "").split())
    if not value:
        return ()
    group = _ALIAS_TO_GROUP.get(value.casefold(), (value,))
    return tuple(dict.fromkeys((value, *group)))


def city_pattern(city: str | None) -> re.Pattern[str] | None:
    """Build a Unicode-aware word-boundary regex for a city and its aliases."""
    variants = city_variants(city)
    if not variants:
        return None
    alternatives = "|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def text_mentions_city(text: str, city: str | None) -> bool:
    pattern = city_pattern(city)
    return bool(pattern and pattern.search(text or ""))
