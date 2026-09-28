"""Checkpoint policy at the real fork/compaction boundary (PR #112864).

Construct real agents from a temporary profile and compact through the real
agent facade. Only the auxiliary model-call boundary returns synthetic text;
the compressor, fork construction, memory manager, and SQLite store are real.
These tests do not run the background-review scheduler or a live provider.
"""

from copy import deepcopy
import json

import pytest


@pytest.fixture
def checkpoint_profile(tmp_path, monkeypatch):
    """Use actual config loading and refuse unexpected outbound HTTP traffic."""
    import httpx
    import requests

    from agent import context_compressor
    from openai.types.chat import ChatCompletion

    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-checkpoint-test-key")
    monkeypatch.chdir(tmp_path)
    config = {
        "model": {
            "default": "gpt-4.1",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "context_length": 128000,
        },
        "compression": {
            "enabled": True,
            "checkpoint_required": True,
            "in_place": True,
            "protect_first_n": 0,
            "protect_last_n": 3,
            "abort_on_summary_failure": True,
        },
        "auxiliary": {
            "compression": {"provider": "auto", "context_length": 128000},
            "background_review": {"provider": "auto"},
        },
    }
    # JSON is valid YAML; both agents read this file through the real loader.
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    unexpected_http = []

    def reject_http(*args, **kwargs):
        unexpected_http.append((args, kwargs))
        raise AssertionError("This regression must not issue an outbound HTTP request")

    monkeypatch.setattr(httpx.Client, "send", reject_http)
    monkeypatch.setattr(requests.Session, "send", reject_http)
    summary_calls = []

    def summarize(**kwargs):
        assert kwargs["task"] == "compression"
        assert kwargs["messages"]
        summary_calls.append(deepcopy(kwargs["messages"]))
        return ChatCompletion(
            id="synthetic-compaction-response",
            created=0,
            model="gpt-4.1",
            object="chat.completion",
            choices=[{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": (
                        "Checkpoint regression summary: Earlier exchanges reviewed "
                        "local revision notes. Preserve the most recent user request "
                        "and continue the review without changing the parent session."
                    ),
                },
            }],
        )

    monkeypatch.setattr(context_compressor, "call_llm", summarize)
    yield home, summary_calls
    assert unexpected_http == []


def _history():
    messages = []
    for index in range(24):
        messages.extend([
            {"role": "user", "content": f"Review revision {index}. " + "Local revision details. " * 240},
            {"role": "assistant", "content": f"Reviewed revision {index}. " + "Local review findings. " * 240},
        ])
    messages.extend([
        {"role": "user", "content": "Keep the parent transcript unchanged and finish this review."},
        {"role": "assistant", "content": "I will finish the review using this independent snapshot."},
    ])
    return messages


def _durable_snapshot(db, session_id):
    return deepcopy((
        db.get_session(session_id),
        db.get_messages(session_id, include_inactive=True),
        db.get_compression_failure_cooldown_row(session_id),
    ))


def _make_agent(db, session_id, *, memory_manager=None):
    from run_agent import AIAgent

    agent = AIAgent(
        model="gpt-4.1",
        provider="openai",
        api_mode="chat_completions",
        base_url="https://api.openai.com/v1",
        api_key="synthetic-checkpoint-test-key",
        platform="cli",
        quiet_mode=True,
        enabled_toolsets=[],
        skip_context_files=True,
        skip_memory=memory_manager is None,
        memory_manager=memory_manager,
        session_db=db,
        session_id=session_id,
    )
    agent._ensure_db_session()
    for message in _history():
        db.append_message(session_id, **message)
    agent._session_messages = db.get_messages_as_conversation(session_id)
    agent._cached_system_prompt = agent._build_system_prompt("Review the local transcript.")
    return agent


def test_real_isolated_fork_compacts_without_rewriting_parent(checkpoint_profile):
    from agent.background_review import build_cache_parity_fork
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, ContextCompressor
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider, PRE_COMPRESS_CHECKPOINT_API_VERSION
    from hermes_state import SessionDB

    class CheckpointProvider(MemoryProvider):
        pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION
        name = "checkpoint-regression-provider"

        def __init__(self):
            self.checkpoints = []

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            self.session_id = session_id

        def get_tool_schemas(self):
            return []

        def on_pre_compress(self, messages, *, require_checkpoint=False):
            self.checkpoints.append((deepcopy(messages), require_checkpoint))
            return "Synthetic checkpoint provider context"

    home, summary_calls = checkpoint_profile
    manager = MemoryManager()
    provider = CheckpointProvider()
    manager.add_provider(provider)
    manager.initialize_all(session_id="parent", hermes_home=str(home), platform="cli")
    db = SessionDB(db_path=home / "state.db")
    parent = fork = None
    try:
        parent = _make_agent(db, "parent", memory_manager=manager)
        assert parent._memory_manager is manager
        assert manager.supports_pre_compress_checkpoint(PRE_COMPRESS_CHECKPOINT_API_VERSION)
        assert parent.compression_checkpoint_required is True
        parent_history = deepcopy(parent._session_messages)
        parent_prompt = parent._cached_system_prompt
        before = _durable_snapshot(db, parent.session_id)

        fork, _, routed = build_cache_parity_fork(parent, max_iterations=1)
        assert routed is False
        assert fork._memory_manager is None
        assert fork._persist_disabled is True
        assert fork._session_db is None
        assert isinstance(fork.context_compressor, ContextCompressor)
        assert fork.context_compressor._session_db is None
        assert fork.context_compressor._session_id == ""
        assert fork.compression_enabled is True
        assert fork.compression_in_place is True
        assert fork.compression_checkpoint_required is True
        assert fork._cached_system_prompt == parent_prompt
        assert fork.tools == parent.tools

        fork_history = deepcopy(parent_history)
        compressed, _ = fork._compress_context(fork_history, parent_prompt, force=True)

        assert len(summary_calls) == 1
        assert any(message.get(COMPRESSED_SUMMARY_METADATA_KEY) for message in compressed)
        assert any("Checkpoint regression summary:" in message.get("content", "") for message in compressed)
        assert len(json.dumps(compressed)) < len(json.dumps(parent_history))
        assert compressed[-2]["content"] == parent_history[-2]["content"]
        assert compressed[-1]["content"] == parent_history[-1]["content"]
        assert fork.compression_checkpoint_required is True
        assert fork.session_id == parent.session_id == "parent"
        assert parent._session_messages == parent_history
        assert parent._cached_system_prompt == parent_prompt
        assert parent.context_compressor._session_db is db
        assert provider.checkpoints == []
        assert _durable_snapshot(db, "parent") == before

        fork.close()
        fork = None
        assert parent._session_messages == parent_history
        assert _durable_snapshot(db, "parent") == before
        assert db.get_session("parent")["ended_at"] is None
    finally:
        if fork is not None:
            fork.close()
        if parent is not None:
            parent.close()
        else:
            manager.shutdown_all()
        db.close()


def test_real_durable_agent_without_memory_provider_still_fails_closed(checkpoint_profile):
    from agent.conversation_compression import CompressionCheckpointUnavailable
    from hermes_state import SessionDB

    home, summary_calls = checkpoint_profile
    db = SessionDB(db_path=home / "state.db")
    agent = None
    try:
        agent = _make_agent(db, "durable")
        assert agent._memory_manager is None
        assert not getattr(agent, "_persist_disabled", False)
        assert agent._session_db is db
        assert agent.compression_checkpoint_required is True
        history = deepcopy(agent._session_messages)
        before = _durable_snapshot(db, "durable")

        with pytest.raises(CompressionCheckpointUnavailable, match="no active provider implements checkpoint API v2"):
            agent._compress_context(agent._session_messages, agent._cached_system_prompt, force=True)

        assert summary_calls == []
        assert agent._session_messages == history
        assert _durable_snapshot(db, "durable") == before
    finally:
        if agent is not None:
            agent.close()
        db.close()
