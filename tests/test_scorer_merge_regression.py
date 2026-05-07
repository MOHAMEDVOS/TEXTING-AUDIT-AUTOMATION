"""Regression tests for same-day merge behavior in ai.scorer."""

from __future__ import annotations

import asyncio

from ai import scorer


class _FakeAsyncPgConn:
    """Minimal asyncpg-like connection for scorer tests."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        if "SELECT id, details FROM audit_scores" in query:
            # Existing same-day row with historical per_conversation payload
            # (does not contain conversation_id by design).
            return {
                "id": 42,
                "details": {
                    "per_conversation": [
                        {
                            "contact": "Existing Contact",
                            "compliance": 90,
                            "sentiment": 90,
                            "professionalism": 90,
                            "script_adherence": 90,
                            "red_flags": [],
                        }
                    ]
                },
            }
        if "SELECT email FROM accounts" in query:
            return {"email": "noah@goccs.net"}
        if "FROM account_assignments" in query:
            return None
        return None

    async def execute(self, query: str, *args):
        self.executed.append((query, args))
        if query.lstrip().startswith("UPDATE"):
            return "UPDATE 1"
        if query.lstrip().startswith("INSERT"):
            return "INSERT 0 1"
        return "OK"


class _AcquireCtx:
    def __init__(self, conn: _FakeAsyncPgConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn: _FakeAsyncPgConn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


class _ConfigConn:
    async def fetchrow(self, *_args, **_kwargs):
        return {
            "email": "noah@goccs.net",
            "funnel_tier": "NF",
            "guidelines": "Test guidelines",
        }

    async def close(self):
        return None


def test_score_merge_writes_current_run_conversation_scores(monkeypatch):
    """Newly scored convos must persist even when same-day row already exists."""

    fake_conn = _FakeAsyncPgConn()
    fake_pool = _FakePool(fake_conn)

    async def _fake_connect(_dsn):
        return _ConfigConn()

    async def _no_sleep(_seconds):
        return None

    def _fake_analyze(_parsed, _agent_name, contact, **kwargs):
        return {
            "conversation_id": kwargs.get("conversation_id"),
            "contact_name": contact,
            "compliance_score": 100,
            "sentiment_score": 90,
            "professionalism_score": 95,
            "script_adherence_score": 100,
            "funnel_stage_reached": "none",
            "pillars_gathered": [],
            "rebuttals_used": [],
            "label_assigned": "WL",
            "label_correct": True,
            "label_should_be": "WL",
            "label_reason": "ok",
            "red_flags": [],
            "actions_triggered": [],
            "summary": "ok",
            "model_used": "groq:test",
        }

    monkeypatch.setattr("asyncpg.connect", _fake_connect)
    monkeypatch.setattr(scorer, "_load_invalid_flag_patterns", lambda _dsn: set())
    monkeypatch.setattr(scorer, "analyze_conversation", _fake_analyze)
    monkeypatch.setattr(scorer.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(scorer, "_check_overdue_unreads", lambda _uc: [])

    conversations = [
        {
            "conversation_id": 9001,
            "contact_name": "New Contact",
            "assigned_labels": ["WL"],
            "parsed_messages": [{"sender": "contact", "message": "hello"}],
        }
    ]

    result = asyncio.run(
        scorer.score_agent_conversations(
            agent_id=1,
            agent_name="Noah",
            conversations=conversations,
            unread_count=0,
            unread_conversations=[],
            pool=fake_pool,
            pinned_key=None,
        )
    )

    insert_calls = [
        args
        for query, args in fake_conn.executed
        if "INSERT INTO conversation_scores" in query
    ]
    assert len(insert_calls) == 1
    assert insert_calls[0][0] == 9001
    assert result.get("conversations_analyzed") == 2  # 1 previous + 1 new (merged details)
    assert result.get("overall_score") is not None
    assert result.get("label_accuracy") == 100.0
