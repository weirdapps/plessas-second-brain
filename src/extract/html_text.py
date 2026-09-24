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
# they show it. iframe, noembed and noframes hold fallback no browser renders,
# which newer Python tokenizers read as raw text, tags and all.
_HIDDEN = {"script", "style", "title", "xml", "template", "iframe", "noembed", "noframes"}

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
# skipped: of the 19,780 bodies in the corpus that opened with a bracket on
# 2026-09-24, 19,779 opened with <html and one with <div. Anchoring on the
# opening keeps out plain text, whatever markup it quotes: a tag anywhere in the
# first 4 KB used to be enough, and a quoted header's <p.petrou@example.com>
# passed for one. The rest of the list is for other sources' documents and
# fragments (Word's <o:p>; XHTML's prolog and stylesheet instructions are
# skipped first). The phrase tags a text mail can open with (b, i, u, a, em,
# strong) and <title> are not on it: converted, such a mail lost its line
# breaks, or its whole body to the title.
LEADING = "\ufeff\u200b\xa0 \t\r\n"
_PROLOG = re.compile(r"(?:<\?xml[^>]*>[\ufeff\u200b\xa0 \t\r\n]*)+", re.IGNORECASE)
_OPENING = re.compile(
    r"<(?:!doctype\s|!--|(?:html|head|body|meta|link|base|style|div|p|br|span|font|center"
    r"|table|tbody|thead|tr|td|th|img|ul|ol|li|h[1-6]|hr|pre|blockquote|section|article"
    r"|header|footer|main|nav|aside|o:p)[\s/>])",
    re.IGNORECASE,
)

# What white-space keeps: the spacing (pre, pre-wrap, break-spaces), the lines
# only (pre-line), or neither (normal, nowrap). These elements keep the spacing
# by default; 271 corpus bodies set it inline to lay plain text out one field a
# line. A void element has no end tag, so a style on one would never end.
_SPACING, _LINES, _NEITHER = 2, 1, 0
_PRE_TAGS = {"pre", "textarea", "xmp", "listing", "plaintext"}
_WHITE_SPACE = re.compile(r"white-space\s*:\s*([a-z-]+)\s*(!\s*important\b)?", re.IGNORECASE)
# Valid, and meaning the parent's value (white-space is inherited)...
_INHERIT = {"inherit", "unset"}
# ...or the browser's own, as if no style set it.
_REVERT = {"revert", "revert-layer"}
_KEEPS = {
    "pre": _SPACING, "pre-wrap": _SPACING, "break-spaces": _SPACING,
    "pre-line": _LINES, "normal": _NEITHER, "nowrap": _NEITHER, "initial": _NEITHER,
}  # fmt: skip
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "source", "wbr",
}  # fmt: skip
# End tags HTML lets a writer leave out: a block ends an open paragraph, and
# these are ended by the next sibling (a cell by the next cell or row, a row by
# the next row or table section), through any inline element left open in
# them, but not past their container. The end of a container ends them as any
# end tag ends what was left open in it.
_SECTIONS = {"tbody", "thead", "tfoot"}
_ENDED_BY = {
    "td": {"td", "th", "tr"} | _SECTIONS, "th": {"td", "th", "tr"} | _SECTIONS,
    "tr": {"tr"} | _SECTIONS, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"},
    "p": _BLOCKS | _PARAGRAPHS,
}  # fmt: skip
_CONTAINER = {
    "td": {"tr", "table"}, "th": {"tr", "table"}, "tr": {"table"} | _SECTIONS,
    "tbody": {"table"}, "thead": {"table"}, "tfoot": {"table"},
}  # fmt: skip
# A block's search for an open paragraph stops at the first element that may
# hold paragraphs, and no search looks deeper than this many open elements.
_HOLDS_P = (_BLOCKS | _PARAGRAPHS | {"td", "th", "body", "html"}) - {"p"}
# An item's or a term's search stops at any of those but a div or an address,
# as a browser's does: a table or a quote inside an item holds its own items.
_CONTAINER.update(dict.fromkeys(("li", "dt", "dd"), _HOLDS_P - {"div", "address"}))
_SEARCH_DEPTH = 32

# The spacing is held as noncharacters, which the whitespace collapse leaves
# alone and no text holds (private-use characters are icon-font glyphs), then
# put back.
_KEEP_SPACING = str.maketrans({" ": "\ufdd0", "\xa0": "\ufdd0", "\t": "\ufdd1"})
_RESTORE_SPACING = str.maketrans({"\ufdd0": " ", "\ufdd1": "\t"})
_NO_PLACEHOLDERS = str.maketrans("", "", "\ufdd0\ufdd1")

# A body that ends inside a hidden element is read again without its opening
# tag, at most this many times: not after a script, which is code even when it
# writes markup, nor after a style that nothing but its own CSS followed.
_REREADS = 3
# What the scan after a stylesheet stops at: a comment, a string, or markup.
_CSS_TOKEN = re.compile(r"/\*|[\"']|<[a-zA-Z/!]")
# A CSS string ends at its quote or, unterminated, at the line's end.
_CSS_STRING = {q: re.compile(rf"{q}(?:[^{q}\\\n]|\\.)*{q}?") for q in ("'", '"')}
# Where a head ends: at its end tag or at the body's start tag.
_HEAD_END = re.compile(r"</head[\s>]|<body[\s/>]", re.IGNORECASE)
# What a head holds besides the hidden elements: an element left open before
# any other element and any text is in the head.
_HEAD = {"html", "head", "meta", "link", "base"}

# A longer address loses its query string (click tracking, mostly), and one
# still longer is left out: newsletters grew twelvefold with every one in full.
_LONGEST_URL = 200

# Safe Links wrappers inside wrappers (mail forwarded between tenants), unwrapped
# at most this deep.
_UNWRAPS = 3


def looks_like_html(content: str | None) -> bool:
    """Whether a body is an HTML document or fragment, not text that quotes markup."""
    if not content:
        return False
    body = content.lstrip(LEADING)
    prolog = _PROLOG.match(body)
    if prolog:
        body = body[prolog.end() :].lstrip(LEADING)
    return _OPENING.match(body) is not None


def _slashes(url: str) -> str:
    """A browser reads a backslash in the host or the path as a slash, which
    urlsplit does not; the query and the fragment keep theirs."""
    cut = min((i for i in (url.find("?"), url.find("#")) if i >= 0), default=len(url))
    return url[:cut].replace("\\", "/") + url[cut:]


def _link_target(attrs) -> str | None:
    """Where a link goes, if to a web page. A Safe Links wrapper is unwrapped to
    its url=, where the click goes; only its real host counts as one, and
    Outlook's originalsrc is not believed, since a sender can write either."""
    # A browser drops tabs and line breaks from an address, which Word wraps.
    url = _slashes(re.sub(r"[\t\r\n]", "", dict(attrs).get("href") or "").strip())
    try:
        for _ in range(_UNWRAPS):
            if not (urlsplit(url).hostname or "").endswith(".safelinks.protection.outlook.com"):
                break
            url = _slashes(parse_qs(urlsplit(url).query).get("url", [""])[0])
        urlsplit(url)
    except ValueError:  # an address urlsplit refuses, like an unclosed IPv6 bracket
        return None
    return url if url.lower().startswith(("http://", "https://")) else None


def _shows(text: str, bare: str) -> bool:
    """Whether a link's text holds its address whole: at a word's start (or after
    www.), and not running on into a longer name, path or address. pal.com is
    not paypal.com, and mybank.co is not mybank.co.uk."""
    return (
        re.search(r"(?:^|[^\w.-]|www\.)" + re.escape(bare) + r"(?![/.@:]?[\w-])", text.lower())
        is not None
    )


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        # Whether the body has started: an element or text the head cannot hold.
        self.in_body = False
        # The outermost hidden element open: its position, its opening tag, and
        # whether it opened in the body.
        self.opened: tuple[tuple[int, int], str, bool] | None = None
        # The open elements, each with what white-space keeps inside it: its
        # own setting, else its parent's, as CSS inherits it.
        self.stack: list[tuple[str, int]] = []
        self.open_count: dict[str, int] = {}
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
        if not _shows(shown, bare):
            self.handle_data(f" ({url}){then}")  # dropped, like any text, when hidden

    def _keeps(self) -> int:
        return self.stack[-1][1] if self.stack else _NEITHER

    def _pop(self) -> None:
        tag, _keeps = self.stack.pop()
        self.open_count[tag] -= 1

    def _end_left_open(self, tag: str) -> None:
        """End what `tag` ends: the nearest open cell, row, item, term or paragraph
        it is the next sibling of (see _ENDED_BY), through any inline element left
        open inside it, but not past its container; then whatever that uncovers
        (a row, after its last cell)."""
        stops = _CONTAINER.get(tag, _HOLDS_P)
        found = True
        while found:
            found = False
            for i in range(len(self.stack) - 1, max(-1, len(self.stack) - 1 - _SEARCH_DEPTH), -1):
                open_tag = self.stack[i][0]
                if tag in _ENDED_BY.get(open_tag, ()):
                    while len(self.stack) > i:
                        self._pop()
                    found = True
                    break
                if open_tag in stops:
                    return

    def _open(self, tag: str, attrs) -> None:
        # As CSS reads it: an invalid value is dropped, an important declaration
        # beats a plain one, and of the rest the last one wins.
        valid = [
            (value.lower(), bool(important))
            for value, important in _WHITE_SPACE.findall(dict(attrs).get("style") or "")
            if value.lower() in _KEEPS or value.lower() in _INHERIT or value.lower() in _REVERT
        ]
        ranked = [value for value, important in valid if important] or [v for v, _ in valid]
        if ranked and ranked[-1] in _INHERIT:
            keeps = self._keeps()
        elif ranked and ranked[-1] in _KEEPS:
            keeps = _KEEPS[ranked[-1]]
        else:  # no style, or one reverted to the browser's own
            keeps = _SPACING if tag in _PRE_TAGS else self._keeps()
        self.stack.append((tag, keeps))
        self.open_count[tag] = self.open_count.get(tag, 0) + 1

    def _close(self, tag: str) -> None:
        if self.open_count.get(tag):  # a stray end tag ends nothing
            while self.stack[-1][0] != tag:
                self._pop()  # left open inside it
            self._pop()

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _HIDDEN:
            if not self.hidden:
                self.opened = (self.getpos(), self.get_starttag_text() or "", self.in_body)
            self.hidden += 1
            return
        if self.hidden:
            return  # what a template or an xml island holds, a <body> included, is never rendered
        if tag not in _HEAD:
            self.in_body = True
        if any(self.open_count.get(kind) for kind in _ENDED_BY):
            self._end_left_open(tag)  # an <hr> too, though it is void
        if tag not in _VOID:
            self._open(tag, attrs)
        if tag == "a":
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
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "head":
            self.in_body = True
        self._close(tag)
        if tag == "a":
            self._end_link()
        elif tag in _PARAGRAPHS or tag in _BLOCKS:
            self._end_link()  # a link left open ends with its block, its address beside it
            self._break(2 if tag in _PARAGRAPHS else 1)

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if data.strip(LEADING):
            self.in_body = True
        data = data.translate(_NO_PLACEHOLDERS)
        keeps = self._keeps()
        if keeps == _SPACING:
            data = data.translate(_KEEP_SPACING)
        elif keeps == _NEITHER:
            # A line break in the source is a space, as in a browser: Outlook
            # wraps its HTML mid-sentence.
            data = data.replace("\r", " ").replace("\n", " ")
        if data.strip():
            self.parts.append(data)
            self.newlines = len(data) - len(data.rstrip("\n"))
        elif "\n" in data:  # blank lines where the lines are kept
            self.parts.append(data)
            self.newlines += data.count("\n")
        elif data:
            self.parts.append(" ")  # between two inline elements, a space is a space

    def close(self) -> None:
        super().close()
        self._end_link()


def _markup_after_css(html: str, pos: int) -> int:
    """Where markup follows the CSS of a stylesheet left open at `pos`: a '<' and
    a letter, '/' or '!' outside its comments and strings; -1 if none does. One
    pass, each step a C search: a regex over the whole rest was quadratic in
    comments that never close, 240 KB of '/* ' taking 40 s, and anyone can send
    that."""
    while (found := _CSS_TOKEN.search(html, pos)) is not None:
        token = found.group()
        if token == "/*":
            end = html.find("*/", found.end())
            if end < 0:
                return -1  # a comment left open runs to the end
            pos = end + 2
        elif token in "'\"":
            string = _CSS_STRING[token].match(html, found.start())  # matches its quote at least
            pos = string.end() if string else found.end()
        else:
            return found.start()
    return -1


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
        # closed hid every word after it. Read it again without it, if the
        # parser's position really points at its tag: a stylesheet up to the
        # markup after its CSS; in the head, up to where the head ends (an
        # island's settings are no text, and whatever else was left open in
        # the head goes with it); in the body, the tag alone, since what
        # follows it there is text. Not a script, which is code even when it
        # writes markup.
        (line, column), tag, in_body = reader.opened
        if tag[1:7].lower() == "script":
            break
        try:
            start = _offset(html, line, column)
        except ValueError:
            break
        rest = start + len(tag)
        if html[start:rest] != tag:
            break
        if tag[1:6].lower() == "style":
            end = _markup_after_css(html, rest)
            if end < 0:
                break  # a stylesheet the body was cut off inside
        elif in_body:
            end = rest
        else:
            head_end = _HEAD_END.search(html, rest)
            end = head_end.start() if head_end else rest
        html = html[:start] + html[end:]
        reader = _read(html)
    text = "".join(reader.parts).replace("\xa0", " ").replace("\ufeff", "")
    lines = (
        " ".join(line.split()).translate(_RESTORE_SPACING).rstrip() for line in text.split("\n")
    )
    # Redacted, as staging redacts the raw HTML: decoding an entity or an unwrapped
    # address can reveal a key the raw markup hid from the patterns.
    return redact_secrets(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n"))
