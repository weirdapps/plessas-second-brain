"""Tests for teams_pipeline.extract_threads — Vertex Claude mocked."""

from unittest.mock import patch

from src.extract.teams_pipeline import extract_threads


def _seed_thread(db, *, msg_count=2, status="pending", with_substantive=True):
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, topic, team_name, first_seen_at)
           VALUES ('19:t', 'channel', 'Channel A', 'Team T', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             message_count, extraction_status, participant_display_names
           ) VALUES (?, 'channel_post', 'P1', '2026-04-29T08:00:00',
                     '2026-04-29T08:30:00', ?, ?, '[\"Alice\",\"Bob\"]')""",
        (chat_id, msg_count, status),
    )
    thread_id = db.execute("SELECT id FROM teams_threads").fetchone()["id"]
    for i in range(msg_count):
        content = (
            "this is a substantive message about quarterly cards strategy"
            if with_substantive
            else "ok"
        )
        db.execute(
            """INSERT INTO teams_messages(
                 teams_message_id, chat_id, thread_id, composed_at, content_text,
                 sender_display_name, is_system
               ) VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (
                f"{chat_id}::M{i}",
                chat_id,
                thread_id,
                f"2026-04-29T08:{i:02d}:00",
                content,
                "Alice" if i == 0 else "Bob",
            ),
        )
    db.commit()
    return thread_id


def _fake_llm_response():
    return (
        '{"summary": "Novak shared Q2 cards roadmap; Papadopoulos approved.",'
        ' "decisions": [{"decision": "Ship Q2 roadmap",'
        ' "decided_by": "Papadopoulos", "decision_date": "2026-04-29"}],'
        ' "action_items": [{"task": "Send roadmap to ExCo",'
        ' "owner": "Novak", "deadline": "2026-05-15"}],'
        ' "key_facts": [{"fact": "Q2 roadmap covers card issuance overhaul"}],'
        ' "sentiment": "positive", "language": "en"}'
    )


def test_extract_threads_inserts_summary_decisions_actions_facts(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    thread_id = _seed_thread(db)

    with patch("src.extract.teams_pipeline._call_llm") as mock:
        mock.return_value = _fake_llm_response()
        result = extract_threads(db, workers=1)

    assert result["extracted"] == 1

    t = db.execute(
        "SELECT summary, sentiment, language, extraction_status FROM teams_threads"
    ).fetchone()
    assert t["extraction_status"] == "extracted"
    assert "Novak" in t["summary"]
    assert t["sentiment"] == "positive"
    assert t["language"] == "en"

    decisions = db.execute(
        "SELECT * FROM decisions WHERE teams_thread_id = ?", (thread_id,)
    ).fetchall()
    assert len(decisions) == 1
    assert "Ship Q2 roadmap" in decisions[0]["decision"]

    actions = db.execute(
        "SELECT * FROM action_items WHERE teams_thread_id = ?", (thread_id,)
    ).fetchall()
    assert len(actions) == 1
    assert actions[0]["owner"] == "Novak"

    facts = db.execute("SELECT * FROM key_facts WHERE teams_thread_id = ?", (thread_id,)).fetchall()
    assert len(facts) == 1


def test_extract_threads_skips_when_too_few_substantive_messages(db, monkeypatch):
    """A 1-message thread of 60 chars (under MIN_SUBSTANTIVE_TOTAL_CHARS=100) is skipped."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    _seed_thread(db, msg_count=1)
    with patch("src.extract.teams_pipeline._call_llm") as mock:
        result = extract_threads(db, workers=1)
        mock.assert_not_called()

    assert result["skipped"] == 1
    status = db.execute("SELECT extraction_status FROM teams_threads").fetchone()[
        "extraction_status"
    ]
    assert status == "skipped"


def test_extract_threads_extracts_long_single_post(db, monkeypatch):
    """A channel-post-style single message above the total-chars floor IS extracted.

    Phase 1 corpus: chatsvcagg /posts returns top-level posts only (no replies),
    so most channel threads are exactly 1 substantive message. This test pins
    the rule that "long single posts" still get extracted.
    """
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, topic, team_name, first_seen_at)
           VALUES ('19:t', 'channel', 'Channel A', 'Team T', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             message_count, extraction_status, participant_display_names
           ) VALUES (?, 'channel_post', 'P1', '2026-04-29T08:00:00',
                     '2026-04-29T08:00:00', 1, 'pending', '[\"Novak, Maria\"]')""",
        (chat_id,),
    )
    thread_id = db.execute("SELECT id FROM teams_threads").fetchone()["id"]
    long_post = (
        "Sharing the Q2 cards roadmap. Key points: (1) issuance overhaul, "
        "(2) revised pricing, (3) channel reactivation. Please review by Friday."
    )
    assert len(long_post) >= 100  # exercises the new total-chars floor
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, thread_id, composed_at, content_text,
             sender_display_name, is_system
           ) VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (
            f"{chat_id}::M0",
            chat_id,
            thread_id,
            "2026-04-29T08:00:00",
            long_post,
            "Novak, Maria",
        ),
    )
    db.commit()

    with patch("src.extract.teams_pipeline._call_llm") as mock:
        mock.return_value = _fake_llm_response()
        result = extract_threads(db, workers=1)

    assert result["extracted"] == 1
    status = db.execute("SELECT extraction_status FROM teams_threads").fetchone()[
        "extraction_status"
    ]
    assert status == "extracted"


def test_extract_threads_marks_failed_on_llm_error(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    _seed_thread(db)
    with patch("src.extract.teams_pipeline._call_llm", side_effect=RuntimeError("vertex 503")):
        result = extract_threads(db, workers=1)

    assert result["failed"] == 1
    t = db.execute("SELECT extraction_status, extraction_error FROM teams_threads").fetchone()
    assert t["extraction_status"] == "failed"
    assert "vertex 503" in t["extraction_error"]


def test_extract_threads_re_extraction_clears_old_decisions(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    thread_id = _seed_thread(db)

    # Pretend a prior extraction left two decisions and one action.
    db.execute(
        "INSERT INTO decisions(decision, teams_thread_id) VALUES ('old1', ?), ('old2', ?)",
        (thread_id, thread_id),
    )
    db.execute(
        "INSERT INTO action_items(task, teams_thread_id) VALUES ('old action', ?)",
        (thread_id,),
    )
    db.commit()

    with patch("src.extract.teams_pipeline._call_llm") as mock:
        mock.return_value = _fake_llm_response()
        extract_threads(db, workers=1)

    decisions = db.execute(
        "SELECT decision FROM decisions WHERE teams_thread_id = ?", (thread_id,)
    ).fetchall()
    assert [d["decision"] for d in decisions] == ["Ship Q2 roadmap"]


def test_extract_threads_respects_limit(db, monkeypatch):
    """limit=N processes only the first N dirty threads, ordered by id."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    # Seed 3 threads in one chat (so each gets a unique anchor_message_id).
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, topic, team_name, first_seen_at)
           VALUES ('19:t', 'channel', 'Channel A', 'Team T', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    for n in range(3):
        db.execute(
            """INSERT INTO teams_threads(
                 chat_id, thread_kind, anchor_message_id, started_at, ended_at,
                 message_count, extraction_status, participant_display_names
               ) VALUES (?, 'channel_post', ?, '2026-04-29T08:00:00',
                         '2026-04-29T08:30:00', 2, 'pending', '[\"Alice\",\"Bob\"]')""",
            (chat_id, f"P{n}"),
        )
    # Add 2 substantive messages per thread.
    threads = [r["id"] for r in db.execute("SELECT id FROM teams_threads ORDER BY id").fetchall()]
    for tid in threads:
        for i in range(2):
            db.execute(
                """INSERT INTO teams_messages(
                     teams_message_id, chat_id, thread_id, composed_at, content_text,
                     sender_display_name, is_system
                   ) VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (
                    f"{chat_id}::T{tid}M{i}",
                    chat_id,
                    tid,
                    f"2026-04-29T08:{i:02d}:00",
                    "this is a substantive message about quarterly cards strategy",
                    "Alice" if i == 0 else "Bob",
                ),
            )
    db.commit()

    with patch("src.extract.teams_pipeline._call_llm") as mock:
        mock.return_value = _fake_llm_response()
        result = extract_threads(db, workers=1, limit=2)

    assert result["extracted"] == 2
    # Exactly 2 of the 3 threads ended up extracted; the third is still pending.
    rows = db.execute("SELECT extraction_status FROM teams_threads ORDER BY id").fetchall()
    statuses = [r["extraction_status"] for r in rows]
    assert statuses.count("extracted") == 2
    assert statuses.count("pending") == 1


def test_extract_threads_tolerates_unknown_json_keys(db, monkeypatch):
    """LLM responses with extra unknown fields don't break extraction."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    _seed_thread(db)

    response_with_extras = (
        '{"summary": "OK", "decisions": [], "action_items": [], '
        '"key_facts": [], "sentiment": "neutral", "language": "en", '
        '"unknown_field": "value", "future_extension": [1, 2, 3]}'
    )
    with patch("src.extract.teams_pipeline._call_llm") as mock:
        mock.return_value = response_with_extras
        result = extract_threads(db, workers=1)

    assert result["extracted"] == 1
    t = db.execute("SELECT summary, sentiment, extraction_status FROM teams_threads").fetchone()
    assert t["extraction_status"] == "extracted"
    assert t["summary"] == "OK"


def test_build_teams_index_skips_when_already_embedded(db, monkeypatch, tmp_path):
    """Thread embedded once should not be re-embedded next run."""
    import numpy as np

    from src.store import embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "e.npz")
    fake_vec = np.zeros((1, embeddings.EMBEDDING_DIM), dtype=np.float32)
    monkeypatch.setattr(embeddings, "generate_embeddings", lambda texts: fake_vec)

    # Seed an extracted thread.
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES ('19:t', 'channel', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             extraction_status, summary, extracted_at
           ) VALUES (?, 'channel_post', 'P1',
                     '2026-04-29T08:00', '2026-04-29T08:30',
                     'extracted', 'sample summary', '2026-04-29T08:31')""",
        (chat_id,),
    )
    db.commit()

    n1 = embeddings.build_teams_index(db)
    n2 = embeddings.build_teams_index(db)
    assert n1 == 1
    assert n2 == 0


def test_build_teams_index_persists_with_correct_offset(db, monkeypatch, tmp_path):
    """The vector lands in the npz and is keyed by id+TEAMS_THREAD_ID_OFFSET."""
    import numpy as np

    from src.store import embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "e.npz")
    fake_vec = np.full((1, embeddings.EMBEDDING_DIM), 0.5, dtype=np.float32)
    monkeypatch.setattr(embeddings, "generate_embeddings", lambda texts: fake_vec)

    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES ('19:t', 'channel', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             extraction_status, summary, extracted_at
           ) VALUES (?, 'channel_post', 'P1',
                     '2026-04-29T08:00', '2026-04-29T08:30',
                     'extracted', 'sample summary', '2026-04-29T08:31')""",
        (chat_id,),
    )
    db.commit()
    seeded_id = db.execute("SELECT id FROM teams_threads").fetchone()["id"]

    embeddings.build_teams_index(db)

    npz = np.load(tmp_path / "e.npz", allow_pickle=False)
    assert embeddings.TEAMS_THREAD_ID_OFFSET - seeded_id in npz["ids"]
    # The vector for that id should equal our fake_vec.
    idx = list(npz["ids"]).index(embeddings.TEAMS_THREAD_ID_OFFSET - seeded_id)
    assert np.allclose(npz["vectors"][idx], 0.5)


def test_build_teams_index_re_embeds_when_extracted_at_is_newer(db, monkeypatch, tmp_path):
    """If extracted_at moves past embedding_at, the thread is re-embedded."""
    import numpy as np

    from src.store import embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "e.npz")
    fake_vec = np.zeros((1, embeddings.EMBEDDING_DIM), dtype=np.float32)
    monkeypatch.setattr(embeddings, "generate_embeddings", lambda texts: fake_vec)

    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES ('19:t', 'channel', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             extraction_status, summary, extracted_at
           ) VALUES (?, 'channel_post', 'P1',
                     '2026-04-29T08:00', '2026-04-29T08:30',
                     'extracted', 'first summary', '2026-04-29T08:31')""",
        (chat_id,),
    )
    db.commit()

    n1 = embeddings.build_teams_index(db)
    assert n1 == 1

    # Simulate re-extraction: extracted_at moves forward past embedding_at.
    db.execute(
        "UPDATE teams_threads SET extracted_at = '2030-01-01T00:00:00+00:00', "
        "summary = 'updated summary'"
    )
    db.commit()

    n2 = embeddings.build_teams_index(db)
    assert n2 == 1, "thread with newer extracted_at should be re-embedded"


def test_build_teams_index_replace_semantics_no_duplicates(db, monkeypatch, tmp_path):
    """Re-embedding the same thread replaces its vector — does not duplicate."""
    import numpy as np

    from src.store import embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "e.npz")
    fake_vec = np.zeros((1, embeddings.EMBEDDING_DIM), dtype=np.float32)
    monkeypatch.setattr(embeddings, "generate_embeddings", lambda texts: fake_vec)

    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES ('19:t', 'channel', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             extraction_status, summary, extracted_at
           ) VALUES (?, 'channel_post', 'P1',
                     '2026-04-29T08:00', '2026-04-29T08:30',
                     'extracted', 'sample', '2026-04-29T08:31')""",
        (chat_id,),
    )
    db.commit()
    seeded_id = db.execute("SELECT id FROM teams_threads").fetchone()["id"]

    embeddings.build_teams_index(db)

    # Force re-embed (force=True clears embedding_at).
    embeddings.build_teams_index(db, force=True)

    npz = np.load(tmp_path / "e.npz", allow_pickle=False)
    target_id = embeddings.TEAMS_THREAD_ID_OFFSET - seeded_id
    occurrences = int((npz["ids"] == target_id).sum())
    assert occurrences == 1, f"expected exactly 1 row for id {target_id}, got {occurrences}"


def test_build_teams_index_returns_zero_when_no_dirty_threads(db, monkeypatch, tmp_path):
    """When there's nothing to embed, the function is a clean no-op (no LLM/Vertex call)."""
    from unittest.mock import MagicMock

    from src.store import embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "e.npz")
    mock_gen = MagicMock()
    monkeypatch.setattr(embeddings, "generate_embeddings", mock_gen)

    n = embeddings.build_teams_index(db)
    assert n == 0
    mock_gen.assert_not_called()


def test_call_llm_uses_shared_client_and_honors_model_override(monkeypatch):
    """_call_llm delegates to the shared client (never builds/closes its own) and
    still honors the BRAIN_TEAMS_MODEL override — model is a per-request arg, so a
    shared project/region-scoped client serves any model."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.extract import teams_pipeline

    fake_client = MagicMock()
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model",
        lambda: (fake_client, "ignored-extract-model"),
    )

    captured = {}

    def fake_create(client, **kwargs):
        captured["client"] = client
        captured["model"] = kwargs.get("model")
        return SimpleNamespace(content=[SimpleNamespace(text="RAW")])

    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", fake_create)
    monkeypatch.setenv("BRAIN_TEAMS_MODEL", "claude-teams-override")
    # Env the OLD (per-call) path needed to build its own client, so the RED is a
    # clean delegation-assertion failure rather than a missing-project error.
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "europe-west1")

    out = teams_pipeline._call_llm("system", "user")

    assert out == "RAW"
    assert captured["client"] is fake_client  # delegated to the shared client
    assert captured["model"] == "claude-teams-override"  # kept teams model override
    fake_client.close.assert_not_called()  # never closes the shared client
