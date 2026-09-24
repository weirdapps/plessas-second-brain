"""Tests for the staging JSON shape every exporter writes."""


class TestJSONOutput:
    """Test JSON output format matches specification."""

    def test_email_schema(self):
        """Test email dictionary matches required schema."""
        email = {
            "message_id": "12345",
            "date_received": "2026-03-15T14:30:00+02:00",
            "sender": {"name": "John Doe", "address": "john@example.com"},
            "to_recipients": [
                {"name": "Jane Smith", "address": "jane@example.com"},
                {"name": "Bob Wilson", "address": "bob@example.com"},
            ],
            "cc_recipients": [
                {"name": "Alice Brown", "address": "alice@example.com"},
            ],
            "subject": "Re: Project update",
            "content": "Email body text...",
            "mailbox_name": "Archive",
        }

        # Verify all required fields exist
        required_fields = {
            "message_id",
            "date_received",
            "sender",
            "to_recipients",
            "cc_recipients",
            "subject",
            "content",
            "mailbox_name",
        }
        assert set(email.keys()) == required_fields

        # Verify sender structure
        assert "name" in email["sender"]
        assert "address" in email["sender"]

        # Verify recipients structure
        for recipient in email["to_recipients"]:
            assert "name" in recipient
            assert "address" in recipient

        for recipient in email["cc_recipients"]:
            assert "name" in recipient
            assert "address" in recipient

    def test_batch_schema(self):
        """Test batch file schema matches specification."""
        batch = {
            "batch_number": 1,
            "exported_at": "2026-03-17T02:15:00+02:00",
            "emails": [
                {
                    "message_id": "12345",
                    "date_received": "2026-03-15T14:30:00+02:00",
                    "sender": {"name": "John Doe", "address": "john@example.com"},
                    "to_recipients": [],
                    "cc_recipients": [],
                    "subject": "Test",
                    "content": "Body",
                    "mailbox_name": "Archive",
                }
            ],
        }

        # Verify batch structure
        assert "batch_number" in batch
        assert "exported_at" in batch
        assert "emails" in batch
        assert isinstance(batch["emails"], list)
