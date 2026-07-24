"""Sent Items ingestion (PR #8).

The export is folder-generic; the sync wrapper now runs a Sent Items pass so
outbound mail (esp. external replies sent without self-CC) is captured. This locks
the folder -> mailbox_name mapping the wrapper relies on.
"""

from src.export.outlook_export import _outlook_to_staging_email


def test_sent_message_maps_to_sent_items_mailbox():
    msg = {
        "Id": "AAA",
        "ReceivedDateTime": "2026-07-19T21:05:36Z",
        "Subject": "re: vendor proposal",
        "From": {
            "EmailAddress": {"Name": "PAPADOPOULOS", "Address": "nikos.papadopoulos@example.com"}
        },
        "ToRecipients": [{"EmailAddress": {"Name": "Vendor", "Address": "v@ext.com"}}],
        "CcRecipients": [],
        "Body": {"Content": "as discussed, we will proceed by Friday"},
        "ConversationId": "c1",
    }
    out = _outlook_to_staging_email(msg, "Sent Items")

    assert out["mailbox_name"] == "Sent Items"
    assert out["message_id"] == "AAA"
    assert out["sender"]["address"] == "nikos.papadopoulos@example.com"  # user is the sender
    assert out["to_recipients"][0]["address"] == "v@ext.com"  # external, no self-CC
    assert out["content"] == "as discussed, we will proceed by Friday"
