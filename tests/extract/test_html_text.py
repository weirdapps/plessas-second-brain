"""HTML email bodies become the text a reader sees.

Outlook bodies arrive as HTML: 19,725 emails held 963 MB of it, 23% of the rows
and two thirds of emails.content. Styles, tables and markup were indexed as
words, and the extraction prompt's 50,000-character cap cut real text in 17% of
the long ones.
"""

from src.extract.html_text import html_to_text, looks_like_html


def test_markup_styles_and_scripts_are_not_text():
    html = (
        "<html><head><style>p { color: red; }</style><title>t</title></head>"
        "<body><script>var x = 1;</script><p>Καλησπέρα,</p><p>the <b>deck</b> is ready.</p>"
        "</body></html>"
    )

    assert html_to_text(html) == "Καλησπέρα,\n\nthe deck is ready."


def test_line_breaks_blocks_and_table_cells_keep_their_shape():
    html = (
        "<div>one<br>two</div><table><tr><td>Q1</td><td>12</td></tr>"
        "<tr><td>Q2</td><td>15</td></tr></table><ul><li>first</li><li>second</li></ul>"
    )

    assert html_to_text(html) == "one\ntwo\n\nQ1 12\nQ2 15\n\nfirst\nsecond"


def test_entities_and_non_breaking_spaces_are_decoded():
    assert html_to_text("<p>A&nbsp;&amp;&nbsp;B &lt;ok&gt; &eacute;&#8364;</p>") == "A & B <ok> é€"
    assert html_to_text("\ufeff\r\n<p>x\ufeffy</p>") == "xy"  # a byte-order mark shows as nothing


def test_outlook_conditional_comments_and_xml_are_dropped():
    html = (
        "<html><head><xml><o:OfficeDocumentSettings/></xml></head><body>"
        "<!--[if gte mso 9]><p>hidden</p><![endif]-->"
        "<p class=MsoNormal>Visible<o:p></o:p></p></body></html>"
    )

    assert html_to_text(html) == "Visible"


def test_blank_runs_collapse():
    assert html_to_text("<p>a</p>\n\n\n<p>   </p><p>b   c</p>") == "a\n\nb c"


def test_plain_text_is_not_mistaken_for_html():
    assert looks_like_html("<html><body>x</body></html>")
    assert looks_like_html("<div>x</div>")
    assert looks_like_html('<!DOCTYPE html><html lang="el">')
    assert looks_like_html("\ufeff\r\n <html><body>x</body></html>")
    assert looks_like_html("<style>" + "p { margin: 0 }" * 400 + "</style><div>x</div>")
    assert looks_like_html('<?xml version="1.0"?>\n<!DOCTYPE html><html><body>x</body></html>')
    assert looks_like_html("<blockquote>x</blockquote>")
    assert looks_like_html("<strong>x</strong> and more")
    assert looks_like_html("<o:p></o:p><p>x</p>")
    assert looks_like_html("\xa0\u200b<html><body>x</body></html>")
    assert not looks_like_html("<mailto:a@example.com> wrote")
    assert not looks_like_html("<https://example.com/a> is the link")
    assert not looks_like_html("Plain text that mentions <angle> brackets")
    assert not looks_like_html("a < b and c > d")
    assert not looks_like_html(None)
    assert not looks_like_html("")


def test_text_quoting_markup_or_addresses_stays_text():
    """A tag anywhere near the top used to be enough, and a quoted header's
    <p.petrou@example.com> passed for one: the text lost every bracketed address."""
    assert not looks_like_html("Call me.\nFrom: Petros <p.petrou@example.com>\nSent: Monday")
    assert not looks_like_html("Please use <br> tags.\nContact: Maria <maria@example.com>")
    assert not looks_like_html("<maria@example.com> wrote:\n> the <p> tag is fine")


def test_a_head_left_open_hides_nothing():
    """</head> is optional, and a <head> never closed hid the whole body."""
    html = '<html><head><meta charset="utf-8"><title>News</title><body><p>Board approved.</p>'

    assert html_to_text(html) == "Board approved."
    assert html_to_text("<html><head><style>p{}</style><body>Hello world</body></html>") == (
        "Hello world"
    )
    # Text left in the head itself shows, as a browser shows it.
    assert html_to_text('<head><meta charset="utf-8">Draft</head><body><p>x</p></body>') == (
        "Draft\n\nx"
    )


def test_an_element_never_closed_hides_nothing_after_it():
    """A <title> or <xml> never closed swallowed every word after it."""
    assert html_to_text("<p>Hi</p><title>x<p>Board approved</p>") == "Hi\n\nx\n\nBoard approved"
    assert html_to_text("<p>a</p><title>b<p>c</p><xml>d<p>e</p>") == "a\n\nb\n\nc\n\nd\n\ne"


def test_an_element_never_closed_hides_itself_when_nothing_follows_it():
    """Re-read only when markup was swallowed: a script cut off at the end of a
    body is code, and stays out of the text."""
    assert html_to_text("<p>hello</p><script>var token = 1;") == "hello"
    assert html_to_text("<p>a</p><title>b<p>c</p><style>d") == "a\n\nb\n\nc"


def test_only_the_first_body_ends_the_head():
    """A browser renders nothing of a <template>, whatever it holds."""
    html = "<body><p>visible</p><template><body>unseen</body></template><p>after</p>"

    assert html_to_text(html) == "visible\n\nafter"
    assert html_to_text("<html><head><xml><o:v>1</o:v><body><p>text</p></body></html>") == "text"


def test_a_position_that_misses_the_tag_is_not_trusted(monkeypatch):
    """The re-read cuts the tag out where the parser says it is. Cutting anywhere
    else would delete text, so a position that misses the tag stops it."""
    from src.extract import html_text

    html = "<p>Hi</p><title>x<p>Board approved</p>"
    monkeypatch.setattr(html_text._Reader, "getpos", lambda self: (1, 0))
    assert html_text.html_to_text(html) == "Hi"
    monkeypatch.setattr(html_text._Reader, "getpos", lambda self: (99, 0))
    assert html_text.html_to_text(html) == "Hi"


def test_noscript_shows_as_a_mail_client_shows_it():
    """Mail clients run no script, so they show what <noscript> holds."""
    html = "<body><noscript>enable js</noscript><p>real text</p></body>"

    assert html_to_text(html) == "enable js\n\nreal text"


def test_a_link_keeps_where_it_goes():
    """The extraction prompt asks for links, and a search for a site should find
    the mail, so a link hidden behind 'here' keeps its address."""
    html = '<p>The deck is <a href="https://contoso.sharepoint.com/sites/x/Q3.pptx">here</a>.</p>'

    assert (
        html_to_text(html) == "The deck is here (https://contoso.sharepoint.com/sites/x/Q3.pptx)."
    )
    assert html_to_text('<a href="https://www.example.com/">www.example.com</a>') == (
        "www.example.com"
    )
    assert html_to_text('<a href="mailto:a@example.com">Petros</a>') == "Petros"
    assert html_to_text('<a href="#top">top</a>') == "top"
    assert (
        html_to_text('<a href="https://example.com/a\r\n/b">x</a>') == "x (https://example.com/a/b)"
    )
    assert html_to_text('<a href=" https://example.com/x ">x</a>') == "x (https://example.com/x)"
    assert (
        html_to_text('<a href="https://example.com/Doc">EXAMPLE.COM/doc</a>') == "EXAMPLE.COM/doc"
    )
    assert html_to_text('<a href="https://www.example.com/">example.com</a>') == "example.com"
    assert html_to_text('<p>see <a href="https://example.com/x">this') == (
        "see this (https://example.com/x)"
    )
    assert html_to_text('<a href="https://a.example/">one<a href="https://b.example/">two</a>') == (
        "one (https://a.example/) two (https://b.example/)"
    )
    # A link left open ends with its paragraph, so its address stays beside it.
    assert html_to_text('<a href="https://a.example/">one</p><p>two</p>') == (
        "one (https://a.example/)\n\ntwo"
    )
    assert html_to_text('<template><a href="https://example.com/t">t</a></template><p>v</p>') == "v"


def test_a_safe_link_reads_as_the_address_it_wraps():
    wrapped = (
        "https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.com"
        "%2Fdoc%3Fid%3D7&amp;data=05%7C02%7C&amp;reserved=0"
    )

    assert (
        html_to_text(f'<a href="{wrapped}">the doc</a>') == "the doc (https://example.com/doc?id=7)"
    )
    assert html_to_text(f'<a href="{wrapped}" originalsrc="https://example.com/a">x</a>') == (
        "x (https://example.com/doc?id=7)"
    )
    mail = "https://eur01.safelinks.protection.outlook.com/?url=mailto%3Aa%40example.com&amp;data=1"
    assert html_to_text(f'<a href="{mail}">Petros</a>') == "Petros"


def test_a_link_shows_where_it_really_goes():
    """Only a real Safe Links host is unwrapped, and only then is Outlook's copy of
    the address (originalsrc) believed: a phishing link must not read as the bank."""
    spoof = "https://evil.example/login?x=.safelinks.protection.outlook.com&amp;url=https://bank.example/"
    assert html_to_text(f'<a href="{spoof}">Bank login</a>') == (
        "Bank login (https://evil.example/login?x=.safelinks.protection.outlook.com"
        "&url=https://bank.example/)"
    )
    html = '<a href="https://evil.example/login" originalsrc="https://bank.example/">Bank login</a>'
    assert html_to_text(html) == "Bank login (https://evil.example/login)"
    assert html_to_text('<a href="http://[::1">x</a>') == "x"  # urlsplit refuses it


def test_a_link_adds_little_to_the_text():
    """An image-only link adds nothing a reader sees, and a click-tracking query
    string is noise: newsletters grew twelvefold with every address in full."""
    assert html_to_text('<a href="https://ex.example/logo-click"><img src="x.png"></a>') == ""
    assert html_to_text('<a href="https://ex.example/"><img alt="Logo"></a>') == (
        "Logo (https://ex.example/)"
    )
    tracked = "https://click.example.com/track?upn=" + "A1b2" * 60
    assert html_to_text(f'<a href="{tracked}">Read more</a>') == (
        "Read more (https://click.example.com/track)"
    )
    assert html_to_text(f'<a href="https://x.example/{"p" * 250}">x</a>') == "x"


def test_decoded_text_is_redacted():
    """Staging redacts the raw HTML, where percent-encoding or an entity can hide
    a key that decoding then reveals, so the text is redacted too."""
    key = "AKIA" + "IOSFODNN7EXAMPLE"
    wrapped = (
        "https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fb.s3.amazonaws.com"
        f"%2Ff.pdf%3FX-Amz-Credential%3D{key}%252F20260101&amp;data=1"
    )
    text = html_to_text(f'<a href="{wrapped}">download</a><p>&#65;{key[1:]} again</p>')

    assert key not in text
    assert text.count("[REDACTED:aws-key-id]") == 2


def test_pre_keeps_its_spacing_and_other_source_breaks_are_spaces():
    """In <pre> the spacing is the layout; elsewhere a line break in the source is
    a space, as a browser shows it (Outlook wraps its HTML mid-sentence)."""
    assert html_to_text("<pre>col1    col2\n  indented</pre>") == "col1    col2\n  indented"
    assert html_to_text("<pre>a  b</pre><p>c    d</p>") == "a  b\n\nc d"
    assert html_to_text("<pre>one<b>\n\n</b>two</pre>") == "one\n\ntwo"
    assert html_to_text("<pre>a\n</pre>b") == "a\nb"
    assert html_to_text("<pre>  first\nsecond  \n</pre>") == "  first\nsecond"
    assert html_to_text("<p>one long\r\nsentence</p>") == "one long sentence"


def test_css_white_space_pre_keeps_its_lines():
    """271 corpus bodies lay out plain text with an inline white-space:pre style,
    one field a line, as a browser shows it."""
    html = '<div style="white-space:pre-wrap">Name:    John\nAmount:  100</div><p>a\nb</p>'

    assert html_to_text(html) == "Name:    John\nAmount:  100\n\na b"
    assert html_to_text('<span style="WHITE-SPACE: pre">x  y</span><b>z  w</b>') == "x  yz w"
    assert html_to_text('<br style="white-space:pre"><p>a\nb</p>') == "a b"  # no end tag
    assert html_to_text("<textarea>a  b\nc</textarea>") == "a  b\nc"


def test_private_use_characters_are_text():
    """Icon fonts put glyphs in the private-use area; they are not spacing."""
    assert html_to_text("<p>\ue000icon\ue001</p><pre>a b</pre>") == "\ue000icon\ue001\n\na b"
    assert html_to_text("<pre>a\ufdd0b</pre>") == "ab"


def test_an_image_reads_as_its_alt_text():
    """Outlook's join buttons and signatures are images; a reader with images off
    sees their alt text, and so should the index."""
    html = '<p>Click <img src="cid:b.png" alt="Join the meeting"> now<img src="x.png"></p>'

    assert html_to_text(html) == "Click Join the meeting now"
