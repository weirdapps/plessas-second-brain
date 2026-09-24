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
    assert not looks_like_html("Plain text that mentions <angle> brackets")
    assert not looks_like_html("a < b and c > d")
    assert not looks_like_html(None)
    assert not looks_like_html("")
