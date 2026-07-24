from src.export.state import (
    OutlookSyncState,
    load_outlook_sync_state,
    save_outlook_sync_state,
)


def test_load_returns_empty_state_when_file_missing(tmp_path):
    s = load_outlook_sync_state(tmp_path / "nope.json")
    assert s.last_seen_received_at is None
    assert s.last_seen_message_id is None
    assert s.consecutive_failures == 0


def test_round_trip(tmp_path):
    p = tmp_path / "state.json"
    s = OutlookSyncState(
        last_sync_started_at="2026-04-22T14:00:00Z",
        last_sync_completed_at="2026-04-22T14:00:43Z",
        last_seen_received_at="2026-04-22T13:58:14Z",
        last_seen_message_id="AAMk-x",
        messages_in_last_run=87,
        consecutive_failures=0,
        schema_version=2,
    )
    save_outlook_sync_state(p, s)
    loaded = load_outlook_sync_state(p)
    assert loaded == s


def test_save_is_atomic(tmp_path):
    p = tmp_path / "state.json"
    save_outlook_sync_state(p, OutlookSyncState(consecutive_failures=0))
    # tmp file must not linger
    assert not (tmp_path / "state.json.tmp").exists()
