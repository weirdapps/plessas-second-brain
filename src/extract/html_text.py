"""HTML email bodies as the text a reader sees.

Outlook bodies arrive as HTML, and the loader kept it: styles, tables and markup
were indexed as words and fed to the extraction prompt, whose character cap then
cut the text that mattered. The markup is kept on the side (email_html) for the
two readers that need it, the SharePoint link scan and the inline-image
positions.
"""

import re
from html.parser import HTMLParser

# Whatever these hold is never shown to a reader. Outlook puts its <xml> and
# <style> blocks in <head>.
_HIDDEN = {"head", "script", "style", "title", "xml", "template", "noscript"}

# On a line of their own, as a browser lays them out...
_BLOCKS = {
    "address", "article", "aside", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "header", "hr", "li", "main", "nav",
    "pre", "section", "tbody", "tfoot", "thead", "tr",
}  # fmt: skip

# ...and these with a blank line around them.
_PARAGRAPHS = {"blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "p", "table", "ul"}

_TAG = re.compile(r"<\s*(html|body|head|div|p|table|br|span|meta|!doctype)\b", re.IGNORECASE)


def looks_like_html(content: str | None) -> bool:
    """Whether a body is HTML markup rather than text that mentions a bracket."""
    return bool(content) and _TAG.search((content or "")[:4096]) is not None


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        # Line breaks at the end of the text so far; the start counts as a
        # paragraph break, so nothing opens with blank lines.
        self.newlines = 2

    def _break(self, count: int) -> None:
        if self.newlines < count:
            self.parts.append("\n" * (count - self.newlines))
            self.newlines = count

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _HIDDEN:
            self.hidden += 1
        elif tag == "br":
            self.parts.append("\n")
            self.newlines += 1
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in _PARAGRAPHS:
            self._break(2)
        elif tag in _BLOCKS:
            self._break(1)

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in _PARAGRAPHS:
            self._break(2)
        elif tag in _BLOCKS:
            self._break(1)

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if data.strip():
            self.parts.append(data)
            self.newlines = 0
        elif data:
            self.parts.append(" ")  # between two inline elements, a space is a space


def html_to_text(html: str) -> str:
    """The visible text of `html`: no markup, entities decoded, one space between
    words, a newline between lines and a blank line between paragraphs."""
    reader = _Reader()
    reader.feed(html)
    reader.close()
    text = "".join(reader.parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
