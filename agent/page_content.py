"""Helpers for extracting visible, semantically marked page content."""

from __future__ import annotations

import copy
import re

from bs4 import BeautifulSoup, Tag

_SPACE_RE = re.compile(r"\s+")
_FOOTER_SELECTORS = (
    "footer",
    '[role="contentinfo"]',
    '[id*="footer" i]',
    '[class*="footer" i]',
)


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).casefold() == "true":
        return True
    style = re.sub(r"\s+", "", str(tag.get("style", "")).casefold())
    return "display:none" in style or "visibility:hidden" in style


def extract_footer_text(raw_html: str) -> str:
    """Extract visible text from semantic or conventionally named footer regions.

    Sites often place addresses and shared inboxes in ``<footer>``, a
    ``contentinfo`` landmark, or an element whose id/class contains ``footer``.
    Hidden and non-rendered descendants are discarded. This is supplemental:
    callers should still inspect the full visible page when no footer marker is
    present.
    """
    if not raw_html:
        return ""
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        regions = soup.select(", ".join(_FOOTER_SELECTORS))
    except Exception:
        return ""

    snippets: list[str] = []
    seen: set[str] = set()
    for region in regions:
        if _is_hidden(region):
            continue
        clone = copy.deepcopy(region)
        for node in clone.find_all(["script", "style", "noscript", "template"]):
            node.decompose()
        for node in list(clone.find_all(True)):
            if _is_hidden(node):
                node.decompose()
        text = _SPACE_RE.sub(" ", clone.get_text(" ", strip=True)).strip()
        key = text.casefold()
        if text and key not in seen:
            snippets.append(text)
            seen.add(key)
    return " ".join(snippets)


__all__ = ["extract_footer_text"]
