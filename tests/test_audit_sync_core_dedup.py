"""A message staged more than once is extracted once per run, from its last copy.

The Outlook export's cursor is inclusive, so a folder's newest message is staged
again every hour until it loads, and a failing one never loads. collect_emails
concatenates every batch and the pending list filtered only on processed_ids,
so in its k-th run such a message went to the model k times, in parallel, and
each copy was counted as a failure of its own.
"""

import pytest

from src.extract import local


@pytest.fixture
def staged(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    calls: list[tuple[str, str]] = []

    def extract(email, api_key, engine="claude"):
        calls.append((email["message_id"], email["subject"]))
        if email["message_id"] == "refused":
            return email["message_id"], None, False, local.FAULT
        return email["message_id"], {"summary": "s"}, False, None

    monkeypatch.setattr(local, "extract_inline", extract)
    return calls


@pytest.mark.parametrize("workers", [1, 3])
def test_each_message_goes_to_the_model_once_from_its_last_copy(staged, monkeypatch, workers):
    emails = [
        {"message_id": "refused", "subject": "first staging"},
        {"message_id": "fresh", "subject": "only copy"},
        {"message_id": "refused", "subject": "second staging"},
        {"message_id": "refused", "subject": "third staging"},
    ]
    monkeypatch.setattr(local, "collect_emails", lambda: emails)

    result = local.run_extraction(workers=workers)

    assert sorted(staged) == [("fresh", "only copy"), ("refused", "third staging")]
    assert result["extracted"] == 1
    assert result["failed"] == 1
