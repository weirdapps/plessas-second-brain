"""Quoted history: where the newest message of a reply ends (issue 164).

Quoted reply chains were 46.5% of the text sent to the model, and each reply re-extracted
the history as new rows. Every name and address here is invented.
"""

import pytest

from src.extract import quotes as q

OUTLOOK_EN = (
    "Thanks, approved. Please go ahead with the March launch.\n"
    "\n"
    "Bob\n"
    "\n"
    "From: Alice Smith <alice@example.com>\n"
    "Sent: Monday, October 5, 2026 9:12 AM\n"
    "To: Bob Jones <bob@example.com>\n"
    "Subject: Okapi launch date\n"
    "\n"
    "Can we launch in March instead of April?\n"
)

OUTLOOK_EL = (
    "Συμφωνώ, προχωράμε με τον Μάρτιο.\n"
    "\n"
    "Από: Ελένη Παππά <eleni@example.com>\n"
    "Στάλθηκε: Δευτέρα, 5 Οκτωβρίου 2026 9:02 π.μ.\n"
    "Προς: Νίκος Αλεξίου <nikos@example.com>\n"
    "Κοιν.: Γιώργος Λάμπρου <giorgos@example.com>\n"
    "Θέμα: Ημερομηνία έναρξης\n"
    "\n"
    "Μπορούμε να ξεκινήσουμε τον Μάρτιο;\n"
)


def test_an_english_outlook_reply_keeps_only_the_newest_message():
    split = q.split_quoted(OUTLOOK_EN)

    assert split.newest == "Thanks, approved. Please go ahead with the March launch.\n\nBob"
    assert split.quoted.startswith("From: Alice Smith")
    assert split.quoted_count == 1


def test_a_greek_outlook_reply_keeps_only_the_newest_message():
    split = q.split_quoted(OUTLOOK_EL)

    assert split.newest == "Συμφωνώ, προχωράμε με τον Μάρτιο."
    assert split.quoted.startswith("Από: Ελένη Παππά")


def test_a_mac_outlook_header_with_date_instead_of_sent_is_a_quote():
    text = (
        "Fine by me.\n\n"
        "From: Alice Smith <alice@example.com>\n"
        "Date: Monday, 5 October 2026 at 09:12\n"
        "To: Bob Jones <bob@example.com>\n"
        "Subject: Re: Okapi\n\nolder\n"
    )

    assert q.split_quoted(text).newest == "Fine by me."


def test_nested_quotes_are_counted_and_cut_at_the_first():
    third = (
        "\n\nFrom: Carol White <carol@example.com>\n"
        "Sent: Sunday, October 4, 2026 6:00 PM\n"
        "To: Alice Smith <alice@example.com>\n"
        "Subject: Okapi launch date\n\nThe first message.\n"
    )
    split = q.split_quoted(OUTLOOK_EN + third + OUTLOOK_EL.split("\n\n", 1)[1])

    assert split.newest.startswith("Thanks, approved.")
    assert split.quoted_count == 3


def test_an_original_message_separator_is_a_quote():
    text = "See below.\n\n-----Original Message-----\nFrom: alice@example.com\nolder text\n"

    assert q.split_quoted(text).newest == "See below."


def test_an_outlook_mobile_underscore_rule_before_the_header_is_dropped():
    text = (
        "OK\n\nGet Outlook for iOS\n"
        "________________________________\n"
        "From: Alice Smith <alice@example.com>\n"
        "Sent: Monday, October 5, 2026 9:12:03 AM\n"
        "To: Bob Jones <bob@example.com>\n"
        "Subject: Okapi\n\nolder\n"
    )

    assert q.split_quoted(text).newest == "OK\n\nGet Outlook for iOS"


def test_a_gmail_attribution_line_is_a_quote_even_wrapped():
    text = (
        "Yes, Thursday works.\n\n"
        "On Mon, Oct 5, 2026 at 9:12 AM Alice Smith <\nalice@example.com> wrote:\n\n"
        "> Does Thursday work?\n"
    )

    assert q.split_quoted(text).newest == "Yes, Thursday works."


def test_a_greek_gmail_attribution_line_is_a_quote():
    text = (
        "Ναι, την Πέμπτη.\n\n"
        "Στις Δευ 5 Οκτ 2026 στις 9:12 π.μ., ο χρήστης Ελένη Παππά <eleni@example.com> έγραψε:\n"
        "> Την Πέμπτη μπορείς;\n"
    )

    assert q.split_quoted(text).newest == "Ναι, την Πέμπτη."


def test_inline_answers_between_quoted_lines_are_not_cut():
    """A reply written between the quoted lines holds its answers below the attribution."""
    text = (
        "Answers inline.\n\n"
        "On Mon, Oct 5, 2026 at 9:12 AM Alice Smith <alice@example.com> wrote:\n"
        "> Can you send the deck?\n"
        "Sent it this morning.\n"
        "> And the budget?\n"
        "Friday.\n"
    )

    assert q.split_quoted(text) is None


def test_a_body_that_only_mentions_from_and_sent_is_not_a_quote():
    text = (
        "From: Monday the team works from the new office.\n"
        "The parcel was sent: yesterday, by courier.\n"
        "Subject to approval, we start in March.\n"
    )

    assert q.split_quoted(text) is None


def test_a_signature_with_contact_lines_is_not_a_quote():
    text = (
        "Please review by Friday.\n\n"
        "Bob Jones\n"
        "Head of Payments\n"
        "Tel: ext. 4410\n"
        "Email: bob@example.com\n"
        "Sent from my iPhone\n"
    )

    assert q.split_quoted(text) is None


def test_a_message_that_is_all_quote_is_left_whole():
    """Nothing new on top (a forward with no comment, a bounce): nothing to keep alone."""
    text = OUTLOOK_EN.split("Bob\n\n", 1)[1]

    assert q.split_quoted(text) is None


@pytest.mark.parametrize(
    "marker",
    [
        '<div id="divRplyFwdMsg" dir="ltr">',
        '<div class="gmail_quote">',
        '<div class="gmail_quote gmail_quote_container">',
        '<div id="appendonsend"></div>',
        '<blockquote type="cite">',
        '<div id="mail-editor-reference-message-container">',
        '<div class="yahoo_quoted">',
    ],
)
def test_html_quote_containers_end_the_newest_message(marker):
    html = (
        "<html><body><p>Approved, go ahead.</p>"
        '<div class="gmail_signature">Bob Jones<br>Head of Payments</div>'
        f"{marker}<p><b>From:</b> Alice Smith</p><p>Can we launch in March?</p>"
        "</div></body></html>"
    )

    split = q.split_quoted(html)

    assert split is not None
    assert split.newest == "Approved, go ahead.\n\nBob Jones\nHead of Payments"
    assert "Can we launch in March?" in split.quoted


def test_an_outlook_html_reply_without_a_marker_is_cut_at_its_header_block():
    html = (
        '<html><body><p class="MsoNormal">Approved.</p>'
        '<div style="border:none;border-top:solid #E1E1E1 1.0pt">'
        '<p class="MsoNormal"><b>From:</b> Alice Smith &lt;alice@example.com&gt;<br>'
        "<b>Sent:</b> Monday, October 5, 2026 9:12 AM<br><b>To:</b> Bob Jones<br>"
        "<b>Subject:</b> Okapi</p></div><p>Can we launch in March?</p></body></html>"
    )

    split = q.split_quoted(html)

    assert split is not None
    assert split.newest == "Approved."


def test_a_gmail_signature_alone_is_not_a_quote():
    html = '<div>Approved.</div><div class="gmail_signature">Bob<br>Payments</div>'

    assert q.split_quoted(html) is None


# --- what the extraction is sent ----------------------------------------------------------


def test_the_body_sent_keeps_a_cut_excerpt_of_the_first_quoted_message():
    long_quote = "Can we launch in March? " * 200
    text = OUTLOOK_EN.replace("Can we launch in March instead of April?", long_quote)

    body = q.strip_quoted(text, subject="Re: Okapi launch date")

    assert body.stripped
    assert body.newest.startswith("Thanks, approved.")
    assert body.text.startswith(body.newest)
    assert "From: Alice Smith" in body.text  # the context the reply answers
    assert len(body.text) < len(body.newest) + q.QUOTED_CONTEXT_CHARS + 400
    assert body.left_out_chars > 0
    assert "left out" in body.text


def test_a_forward_is_never_stripped():
    """What a forward passes on was never extracted from its own email."""
    for subject in ("FW: Okapi", "Fwd: Okapi", "Πρ: Okapi", "ΠΡ: Okapi", "Προώθηση: Okapi"):
        body = q.strip_quoted(OUTLOOK_EN, subject=subject)
        assert not body.stripped
        assert body.text == OUTLOOK_EN


def test_a_gmail_forward_marker_is_never_stripped():
    text = (
        "FYI\n\n---------- Forwarded message ---------\n"
        "From: Alice Smith <alice@example.com>\nDate: Mon, Oct 5, 2026\n"
        "Subject: Okapi\nTo: Bob Jones <bob@example.com>\n\nThe regulation text.\n"
    )

    body = q.strip_quoted(text, subject="Okapi")

    assert not body.stripped
    assert body.text == text


def test_an_outlook_html_forward_container_is_never_stripped():
    html = (
        '<p>FYI</p><div id="divRplyFwdMsg"><b>From:</b> Alice<br><b>Sent:</b> Monday<br>'
        "<b>Subject:</b> FW: Okapi</div><p>The regulation text.</p>"
    )

    assert not q.strip_quoted(html, subject="FW: Okapi").stripped


def test_a_body_with_no_quote_comes_back_as_its_text():
    body = q.strip_quoted("<p>Just a note.</p>", subject="Okapi")

    assert not body.stripped
    assert body.text == "Just a note."
    assert body.newest == "Just a note."


def test_a_short_quote_is_sent_whole_since_leaving_it_out_saves_nothing():
    body = q.strip_quoted(OUTLOOK_EN, subject="RE: Okapi launch date")

    assert not body.stripped
    assert body.text == OUTLOOK_EN
    assert body.newest.startswith("Thanks, approved.")
