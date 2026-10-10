"""Check that the translated READMEs retain the English README's technical contract.

Run from any directory: python tools/check_readme_translations.py
This checks structure and links; fluent review is still needed for meaning.
"""

from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("pl", "de", "fr", "es", "pt-BR", "zh-CN", "ja", "ko", "ru", "ar")
LANGUAGE_FILES = ("README.md", *(f"README.{locale}.md" for locale in LOCALES))
SOURCE_MARKER = re.compile(r"<!-- English README SHA-256: ([0-9a-f]{64}) -->")
FENCE = re.compile(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$")
INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)")
HTML_LINK = re.compile(r"\b(?:href|src)=\"([^\"]+)\"")
TABLE_ROW = re.compile(r"(?m)^\|[^\n]+\|[ \t]*$")
H2 = re.compile(r"(?m)^## [^\n]+$")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def targets(markdown: str) -> Counter[str]:
    return Counter((*MARKDOWN_LINK.findall(markdown), *HTML_LINK.findall(markdown)))


def check_local_targets(name: str, links: Counter[str]) -> None:
    for target in links:
        url = urlsplit(target.replace("&amp;", "&"))
        if url.scheme or url.netloc or not url.path:
            continue
        path = ROOT / unquote(url.path)
        require(path.is_file(), f"{name}: missing local target {target}")


def check() -> None:
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    expected_links = targets(source)
    menu = Counter({name: 1 for name in LANGUAGE_FILES})
    for name in LANGUAGE_FILES:
        require(expected_links[name] == 1, f"README.md: missing or duplicated language link {name}")
    require(sum(expected_links[name] for name in menu) == len(menu),
            "README.md: language menu is incomplete")
    check_local_targets("README.md", expected_links)
    source_fences = FENCE.findall(source)
    source_inline = Counter(INLINE_CODE.findall(source))
    source_sections = len(H2.findall(source))
    source_rows = len(TABLE_ROW.findall(source))
    require((len(source_fences), source_sections) == (6, 6),
            "README.md: expected six code blocks and six sections")

    for locale in LOCALES:
        name = f"README.{locale}.md"
        markdown = (ROOT / name).read_text(encoding="utf-8")
        marker = SOURCE_MARKER.search(markdown)
        require(marker is not None and marker.group(1) == digest,
                f"{name}: English README changed; update and review this translation, then refresh its source hash")
        require(FENCE.findall(markdown) == source_fences,
                f"{name}: code blocks differ from English")
        require(Counter(INLINE_CODE.findall(markdown)) == source_inline,
                f"{name}: inline commands or technical identifiers differ from English")
        links = targets(markdown)
        require(links == expected_links, f"{name}: link targets differ from English")
        require(len(H2.findall(markdown)) == source_sections,
                f"{name}: section count differs from English")
        require(len(TABLE_ROW.findall(markdown)) == source_rows,
                f"{name}: table row count differs from English")
        require('<a id="quick-start"></a>' in markdown,
                f"{name}: quick-start anchor missing")
        check_local_targets(name, links)
        print(f"Checked: {name}")


if __name__ == "__main__":
    check()
