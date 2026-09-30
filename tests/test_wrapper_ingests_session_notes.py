"""The conversation sync stores session notes after it exports the conversations, and a
failure there fails the run like the other two steps."""

from pathlib import Path

WRAPPER = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "wrappers"
    / "systemd"
    / "sb-conversation-sync.sh"
)


def test_the_conversation_sync_ingests_session_notes():
    text = WRAPPER.read_text()
    assert "src.cli ingest-session-notes" in text
    assert text.index("export-conversations") < text.index("ingest-session-notes")
    assert "notes_rc" in text[text.index('if [ "$export_rc"') :]
