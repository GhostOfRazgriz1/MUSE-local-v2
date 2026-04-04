"""Unit tests for SessionStore."""

from muse.kernel.session_store import SessionStore


def test_initial_state():
    store = SessionStore()
    assert store.session_id is None
    assert store.conversation_history == []
    assert store.user_tz == "UTC"
    assert store.mood == "resting"
    assert store.llm_calls_count == 0


def test_track_llm_usage():
    store = SessionStore()
    store.track_llm_usage(100, 50)
    assert store.llm_calls_count == 1
    assert store.llm_tokens_in == 100
    assert store.llm_tokens_out == 50
    store.track_llm_usage(200, 100)
    assert store.llm_calls_count == 2
    assert store.llm_tokens_in == 300
    assert store.llm_tokens_out == 150


def test_reset_session():
    store = SessionStore()
    store.session_id = "old-session"
    store.conversation_history = [{"role": "user", "content": "hi"}]
    store.mood = "working"
    store.track_llm_usage(100, 50)

    store.reset_session("new-session")
    assert store.session_id == "new-session"
    assert store.conversation_history == []
    assert store.llm_calls_count == 0
    assert store.llm_tokens_in == 0
    # mood is not reset by reset_session (persists across sessions)


def test_reset_llm_usage():
    store = SessionStore()
    store.track_llm_usage(100, 50)
    store.reset_llm_usage()
    assert store.llm_calls_count == 0
    assert store.llm_tokens_in == 0
    assert store.llm_tokens_out == 0
