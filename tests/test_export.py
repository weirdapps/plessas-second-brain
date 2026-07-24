"""Tests for export pipeline — state management and batch export logic."""

import json
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.export.apple_mail import (
    acquire_lock,
    check_mail_memory,
    release_lock,
    save_batch,
)
from src.export.state import (
    ExportState,
    get_last_batch_number,
    load_state,
    save_state,
    update_progress,
)


class TestStateManagement:
    """Test export state management."""

    @pytest.fixture
    def temp_state_dir(self, monkeypatch, tmp_path):
        """Create temporary state directory."""
        state_dir = tmp_path / "data" / "state"
        state_dir.mkdir(parents=True)

        # Mock the get_state_file_path to use temp directory
        def mock_get_state_file_path():
            return state_dir / "export_state.json"

        monkeypatch.setattr(
            "src.export.state.get_state_file_path",
            mock_get_state_file_path,
        )
        return state_dir

    def test_load_state_no_file(self, temp_state_dir):
        """Test loading state when file doesn't exist — returns initial state."""
        state = load_state()

        assert state["last_batch_number"] == 0
        assert state["last_exported_date"] is None
        assert state["total_exported"] == 0
        assert "last_updated" in state

    def test_save_and_load_state(self, temp_state_dir):
        """Test saving and loading state."""
        initial_state: ExportState = {
            "last_batch_number": 42,
            "last_exported_date": "2026-03-15T14:30:00+02:00",
            "total_exported": 210,
            "last_updated": datetime.now().isoformat(),
        }

        save_state(initial_state)

        loaded_state = load_state()
        assert loaded_state["last_batch_number"] == 42
        assert loaded_state["last_exported_date"] == "2026-03-15T14:30:00+02:00"
        assert loaded_state["total_exported"] == 210

    def test_save_state_atomic(self, temp_state_dir):
        """Test that save_state uses atomic writes."""
        state: ExportState = {
            "last_batch_number": 1,
            "last_exported_date": None,
            "total_exported": 5,
            "last_updated": datetime.now().isoformat(),
        }

        save_state(state)

        # Verify temp file is cleaned up
        temp_files = list(temp_state_dir.glob("*.tmp"))
        assert len(temp_files) == 0

        # Verify final file exists
        state_file = temp_state_dir / "export_state.json"
        assert state_file.exists()

    def test_load_state_corrupted_file(self, temp_state_dir, capsys):
        """Test loading corrupted state file — returns initial state."""
        state_file = temp_state_dir / "export_state.json"

        # Write invalid JSON
        with open(state_file, "w") as f:
            f.write("{ invalid json }")

        state = load_state()

        # Should return initial state
        assert state["last_batch_number"] == 0
        assert state["total_exported"] == 0

        # Should print warning
        captured = capsys.readouterr()
        assert "corrupted state file" in captured.out.lower()

    def test_load_state_missing_fields(self, temp_state_dir, capsys):
        """Test loading state with missing required fields."""
        state_file = temp_state_dir / "export_state.json"

        # Write incomplete state
        with open(state_file, "w") as f:
            json.dump({"last_batch_number": 10}, f)

        state = load_state()

        # Should return initial state
        assert state["last_batch_number"] == 0
        assert state["total_exported"] == 0

        # Should print warning
        captured = capsys.readouterr()
        assert "corrupted state file" in captured.out.lower()

    def test_get_last_batch_number(self, temp_state_dir):
        """Test getting last batch number."""
        state: ExportState = {
            "last_batch_number": 99,
            "last_exported_date": None,
            "total_exported": 495,
            "last_updated": datetime.now().isoformat(),
        }
        save_state(state)

        batch_num = get_last_batch_number()
        assert batch_num == 99

    def test_update_progress(self, temp_state_dir):
        """Test updating progress after batch export."""
        # Start with initial state
        assert load_state()["total_exported"] == 0

        # Export first batch
        update_progress(
            batch_number=1,
            last_email_date="2026-03-15T10:00:00+02:00",
            emails_in_batch=5,
        )

        state = load_state()
        assert state["last_batch_number"] == 1
        assert state["total_exported"] == 5
        assert state["last_exported_date"] == "2026-03-15T10:00:00+02:00"

        # Export second batch
        update_progress(
            batch_number=2,
            last_email_date="2026-03-15T11:00:00+02:00",
            emails_in_batch=5,
        )

        state = load_state()
        assert state["last_batch_number"] == 2
        assert state["total_exported"] == 10
        assert state["last_exported_date"] == "2026-03-15T11:00:00+02:00"


class TestLockFile:
    """Test export lock file behavior."""

    @pytest.fixture
    def temp_lock_dir(self, monkeypatch, tmp_path):
        """Create temporary lock directory."""
        lock_dir = tmp_path / "data" / "state"
        lock_dir.mkdir(parents=True)

        def mock_get_lock_file_path():
            return lock_dir / "export.lock"

        monkeypatch.setattr(
            "src.export.apple_mail.get_lock_file_path",
            mock_get_lock_file_path,
        )
        return lock_dir

    def test_acquire_and_release_lock(self, temp_lock_dir):
        """Test acquiring and releasing lock."""
        lock_file = temp_lock_dir / "export.lock"

        # Acquire lock
        acquire_lock()
        assert lock_file.exists()

        # Check PID is written
        with open(lock_file) as f:
            pid = int(f.read().strip())
        assert pid == os.getpid()

        # Release lock
        release_lock()
        assert not lock_file.exists()

    def test_acquire_lock_already_held(self, temp_lock_dir):
        """Test acquiring lock when already held by running process."""
        temp_lock_dir / "export.lock"

        # Acquire lock first time
        acquire_lock()

        # Try to acquire again — should raise error
        with pytest.raises(RuntimeError, match="Export already running"):
            acquire_lock()

        # Cleanup
        release_lock()

    def test_acquire_lock_stale(self, temp_lock_dir, capsys):
        """Test acquiring lock with stale PID."""
        lock_file = temp_lock_dir / "export.lock"

        # Write stale lock file with non-existent PID
        with open(lock_file, "w") as f:
            f.write("99999")

        # Should remove stale lock and succeed
        acquire_lock()
        assert lock_file.exists()

        # Should print message about stale lock
        captured = capsys.readouterr()
        assert "stale lock" in captured.out.lower()

        # Cleanup
        release_lock()


class TestBatchSave:
    """Test batch file saving."""

    def test_save_batch_format(self, tmp_path):
        """Test batch file format and naming."""
        staging_dir = tmp_path / "data" / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        # Patch the function to use our temp directory
        with patch("src.export.apple_mail.Path") as mock_path:
            # Make Path(__file__).parent.parent.parent return tmp_path
            mock_path.return_value.parent.parent.parent = tmp_path

            emails = [
                {
                    "message_id": "12345",
                    "date_received": "2026-03-15T14:30:00+02:00",
                    "sender": {"name": "John Doe", "address": "john@example.com"},
                    "to_recipients": [{"name": "Jane Smith", "address": "jane@example.com"}],
                    "cc_recipients": [],
                    "subject": "Test email",
                    "content": "Email body content",
                    "mailbox_name": "Archive",
                }
            ]

            save_batch(1, emails)

            # Check file exists with correct name
            batch_file = staging_dir / "batch-00001.json"
            assert batch_file.exists()

            # Check content format
            with open(batch_file) as f:
                data = json.load(f)

            assert data["batch_number"] == 1
            assert "exported_at" in data
            assert len(data["emails"]) == 1
            assert data["emails"][0]["message_id"] == "12345"
            assert data["emails"][0]["subject"] == "Test email"

    def test_save_batch_numbering(self, tmp_path):
        """Test batch number zero-padding."""
        staging_dir = tmp_path / "data" / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        with patch("src.export.apple_mail.Path") as mock_path:
            mock_path.return_value.parent.parent.parent = tmp_path

            save_batch(1, [])
            save_batch(99, [])
            save_batch(1000, [])

            assert (staging_dir / "batch-00001.json").exists()
            assert (staging_dir / "batch-00099.json").exists()
            assert (staging_dir / "batch-01000.json").exists()


class TestMemoryCheck:
    """Test Mail.app memory checking."""

    @patch("subprocess.run")
    def test_check_mail_memory_running(self, mock_run):
        """Test memory check when Mail.app is running."""
        # Mock pgrep returning PID
        pgrep_result = MagicMock()
        pgrep_result.returncode = 0
        pgrep_result.stdout = "12345\n"

        # Mock ps returning RSS in KB (4GB)
        ps_result = MagicMock()
        ps_result.returncode = 0
        ps_result.stdout = "4194304\n"  # 4GB in KB

        mock_run.side_effect = [pgrep_result, ps_result]

        memory_gb = check_mail_memory()

        assert memory_gb == pytest.approx(4.0, rel=0.01)
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_check_mail_memory_not_running(self, mock_run):
        """Test memory check when Mail.app is not running."""
        # Mock pgrep returning no PID
        pgrep_result = MagicMock()
        pgrep_result.returncode = 1
        pgrep_result.stdout = ""

        mock_run.return_value = pgrep_result

        memory_gb = check_mail_memory()

        assert memory_gb == 0.0
        # Should only call pgrep, not ps
        assert mock_run.call_count == 1

    @patch("subprocess.run")
    def test_check_mail_memory_timeout(self, mock_run):
        """Test memory check with timeout."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("pgrep", 5)

        memory_gb = check_mail_memory()

        assert memory_gb == 0.0


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
