"""Check translated README examples, language menus, and local links."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("pl", "de", "fr", "es", "pt-BR", "zh-CN", "ja", "ko", "ru", "ar")
LANGUAGE_FILES = ("README.md", *(f"translations/README.{locale}.md" for locale in LOCALES))
FENCE = re.compile(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$")
INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)")
HTML_LINK = re.compile(r"\b(?:href|src)=\"([^\"]+)\"")
H2 = re.compile(r"(?m)^## [^\n]+$")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def targets(markdown: str) -> Counter[str]:
    return Counter((*MARKDOWN_LINK.findall(markdown), *HTML_LINK.findall(markdown)))


def normalized_targets(name: str, links: Counter[str]) -> Counter[str]:
    normalized = Counter()
    for target in links:
        url = urlsplit(target.replace("&amp;", "&"))
        if url.scheme or url.netloc or not url.path:
            normalized[target] += links[target]
            continue
        path = ((ROOT / name).parent / unquote(url.path)).resolve()
        require(path.is_file(), f"{name}: missing local target {target}")
        normalized[str(path.relative_to(ROOT)) + (f"#{url.fragment}" if url.fragment else "")] += links[target]
    return normalized


def check() -> None:
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    expected_links = targets(source)
    menu = Counter({name: 1 for name in LANGUAGE_FILES})
    for name in LANGUAGE_FILES:
        require(expected_links[name] == 1, f"README.md: missing or duplicated language link {name}")
    require(sum(expected_links[name] for name in menu) == len(menu),
            "README.md: language menu is incomplete")
    expected_resolved = normalized_targets("README.md", expected_links)
    source_fences = FENCE.findall(source)
    source_inline = Counter(INLINE_CODE.findall(source))
    source_sections = len(H2.findall(source))
    require((len(source_fences), source_sections) == (2, 3),
            "README.md: expected two code blocks and three sections")

    for locale in LOCALES:
        name = f"translations/README.{locale}.md"
        markdown = (ROOT / name).read_text(encoding="utf-8")
        require(FENCE.findall(markdown) == source_fences,
                f"{name}: code blocks differ from English")
        require(Counter(INLINE_CODE.findall(markdown)) == source_inline,
                f"{name}: inline commands or technical identifiers differ from English")
        links = targets(markdown)
        require(normalized_targets(name, links) == expected_resolved,
                f"{name}: link targets differ from English")
        require(len(H2.findall(markdown)) == source_sections,
                f"{name}: section count differs from English")
        require('<a id="quick-start"></a>' in markdown,
                f"{name}: quick-start anchor missing")
        print(f"Checked: {name}")


if __name__ == "__main__":
    check()
