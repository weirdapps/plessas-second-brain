"""HTML email bodies as the text a reader sees.

Outlook bodies arrive as HTML, and the loader kept it: styles, tables and markup
were indexed as words and fed to the extraction prompt, whose character cap then
cut the text that mattered. The markup is kept on the side (email_html) for the
two readers that need it, the SharePoint link scan and the inline-image
positions.
"""

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from src.redact import redact_secrets

# Whatever these hold is never shown to a reader. Not <head>: the elements in it
# that hold text are all here, and a <head> never closed (the closing tag is
# optional) hid the whole body. Not <noscript>: mail clients run no script, so
# they show it.
_HIDDEN = {"script", "style", "title", "xml", "template"}

# On a line of their own, as a browser lays them out...
_BLOCKS = {
    "address", "article", "aside", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "header", "hr", "li", "main", "nav",
    "pre", "section", "tbody", "tfoot", "thead", "tr",
    "listing", "plaintext", "textarea", "xmp",
}  # fmt: skip

# ...and these with a blank line around them.
_PARAGRAPHS = {"blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "p", "table", "ul"}

# What an HTML body opens with, once a byte-order mark and blank lines are
# skipped: of the 19,774 bodies in the corpus that open with a bracket, 19,773
# open with <html and one with <div. Anchoring on the opening keeps out plain
# text, whatever markup it quotes: a tag anywhere in the first 4 KB used to be
# enough, and a quoted header's <p.petrou@example.com> passed for one. The rest
# of the list is for other sources' documents and fragments (XHTML's prolog,
# Word's <o:p>); a body that opens with text is text.
LEADING = "\ufeff\u200b\xa0 \t\r\n"
_OPENING = re.compile(
    r"<(?:\?xml\s|!doctype\s|!--|(?:html|head|body|meta|link|base|title|style|div|p|br"
    r"|span|table|tbody|thead|tr|td|th|font|center|a|b|i|u|strong|em|img|ul|ol|li|h[1-6]"
    r"|hr|pre|blockquote|section|article|header|footer|main|nav|aside|[a-z]+:[a-z]+)[\s/>])",
    re.IGNORECASE,
)

# Elements whose spacing is the layout: these, and any with a white-space: pre
# style (271 corpus bodies lay out plain text that way, one field a line). A
# void element has no end tag, so a style on one would never end.
_PRE_TAGS = {"pre", "textarea", "xmp", "listing", "plaintext"}
_PRE_STYLE = re.compile(r"white-space\s*:\s*pre", re.IGNORECASE)
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}

# Their spaces and tabs are held as noncharacters, which the whitespace collapse
# leaves alone and no text holds (private-use characters are icon-font glyphs),
# then put back.
_KEEP_SPACING = str.maketrans({" ": "\ufdd0", "\xa0": "\ufdd0", "\t": "\ufdd1"})
_RESTORE_SPACING = str.maketrans({"\ufdd0": " ", "\ufdd1": "\t"})
_NO_PLACEHOLDERS = str.maketrans("", "", "\ufdd0\ufdd1")

# A body that ends inside a hidden element is read again without its opening
# tag, at most this many times, when markup followed the tag.
_REREADS = 3
_MARKUP = re.compile(r"<[a-zA-Z/!]")

# A longer address loses its query string (click tracking, mostly), and one
# still longer is left out: newsletters grew twelvefold with every one in full.
_LONGEST_URL = 200


def looks_like_html(content: str | None) -> bool:
    """Whether a body is an HTML document or fragment, not text that quotes markup."""
    if not content:
        return False
    return _OPENING.match(content.lstrip(LEADING)) is not None


def _link_target(attrs) -> str | None:
    """Where a link goes, if to a web page. A Safe Links wrapper is unwrapped to
    its url=, where the click goes; only its real host counts as one, and
    Outlook's originalsrc is not believed, since a sender can write either."""
    # A browser drops tabs and line breaks from an address; Word wraps them.
    url = re.sub(r"[\t\r\n]", "", dict(attrs).get("href") or "").strip()
    try:
        if (urlsplit(url).hostname or "").endswith(".safelinks.protection.outlook.com"):
            url = parse_qs(urlsplit(url).query).get("url", [""])[0]
        urlsplit(url)
    except ValueError:  # an address urlsplit refuses, like an unclosed IPv6 bracket
        return None
    return url if url.lower().startswith(("http://", "https://")) else None


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        # The outermost hidden element open: its position and its opening tag.
        self.opened: tuple[tuple[int, int], str] | None = None
        self.bodied = False
        # The open elements that keep their spacing (see _PRE_TAGS).
        self.pre: list[str] = []
        # The link being read: where it goes, and where its text starts in parts.
        self.link: tuple[str | None, int] | None = None
        # Line breaks at the end of the text so far; the start counts as a
        # paragraph break, so nothing opens with blank lines.
        self.newlines = 2

    def _break(self, count: int) -> None:
        if self.newlines < count:
            self.parts.append("\n" * (count - self.newlines))
            self.newlines = count

    def _end_link(self, then: str = "") -> None:
        """After a link's text, the address, unless the text already shows it;
        `then` goes after the address."""
        if self.link is None:
            return
        url, start = self.link
        self.link = None
        shown = "".join(self.parts[start:])
        if url is None or not shown.strip():  # an image with no alt text shows nothing
            return
        if len(url) > _LONGEST_URL:
            parts = urlsplit(url)
            url = f"{parts.scheme}://{parts.netloc}{parts.path}"
            if len(url) > _LONGEST_URL:
                return
        bare = re.sub(r"^https?://(?:www\.)?", "", url, flags=re.IGNORECASE).rstrip("/").lower()
        if bare not in shown.lower():
            self.handle_data(f" ({url}){then}")  # dropped, like any text, when hidden

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _HIDDEN:
            if not self.hidden:
                self.opened = (self.getpos(), self.get_starttag_text() or "")
            self.hidden += 1
            return
        if tag not in _VOID and (
            tag in _PRE_TAGS or _PRE_STYLE.search(dict(attrs).get("style") or "")
        ):
            self.pre.append(tag)
        if tag == "body":
            if not self.bodied:  # the head ends where the body starts, whatever it left open
                self.hidden = 0
                self.bodied = True
        elif tag == "a":
            self._end_link(" ")  # a link inside a link closes the first, as in a browser
            self.link = (_link_target(attrs), len(self.parts))
        elif tag == "br":
            self.parts.append("\n")
            self.newlines += 1
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag == "img":
            # What a reader with images off sees: Outlook's join buttons and
            # many signatures are images.
            alt = dict(attrs).get("alt") or ""
            if alt.strip():
                self.handle_data(f" {alt} ")
        elif tag in _PARAGRAPHS:
            self._break(2)
        elif tag in _BLOCKS:
            self._break(1)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.pre:  # and whatever opened inside it and was left open
            del self.pre[len(self.pre) - 1 - self.pre[::-1].index(tag) :]
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag == "a":
            self._end_link()
        elif tag in _PARAGRAPHS or tag in _BLOCKS:
            self._end_link()  # a link left open ends with its block, its address beside it
            self._break(2 if tag in _PARAGRAPHS else 1)

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        data = data.translate(_NO_PLACEHOLDERS)
        if self.pre:
            data = data.translate(_KEEP_SPACING)
        else:
            # A line break in the source is a space, as in a browser: Outlook
            # wraps its HTML mid-sentence.
            data = data.replace("\r", " ").replace("\n", " ")
        if data.strip():
            self.parts.append(data)
            self.newlines = len(data) - len(data.rstrip("\n"))
        elif "\n" in data:  # blank lines inside <pre>
            self.parts.append(data)
            self.newlines += data.count("\n")
        elif data:
            self.parts.append(" ")  # between two inline elements, a space is a space

    def close(self) -> None:
        super().close()
        self._end_link()


def _read(html: str) -> _Reader:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return reader


def _offset(text: str, line: int, column: int) -> int:
    """The index in `text` of an HTMLParser position (lines count from 1)."""
    start = 0
    for _ in range(line - 1):
        start = text.index("\n", start) + 1
    return start + column


def html_to_text(html: str) -> str:
    """The visible text of `html`: no markup, entities decoded, one space between
    words (the spacing of <pre> kept), a newline between lines, a blank line
    between paragraphs, and after a link's text the address it goes to."""
    reader = _read(html)
    for _ in range(_REREADS):
        if not reader.hidden or reader.opened is None:
            break
        # The body ended inside a hidden element: a <title> or <xml> never
        # closed hid every word after it. Read it again without that tag, if
        # the parser's position really points at it.
        (line, column), tag = reader.opened
        try:
            start = _offset(html, line, column)
        except ValueError:
            break
        if html[start : start + len(tag)] != tag:
            break
        if not _MARKUP.search(html, start + len(tag)):
            break  # only its own content followed, cut off: a script is code
        html = html[:start] + html[start + len(tag) :]
        reader = _read(html)
    text = "".join(reader.parts).replace("\xa0", " ").replace("\ufeff", "")
    lines = (
        " ".join(line.split()).translate(_RESTORE_SPACING).rstrip() for line in text.split("\n")
    )
    # Redacted, as staging redacts the raw HTML: decoding an entity or an unwrapped
    # address can reveal a key the raw markup hid from the patterns.
    return redact_secrets(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n"))
