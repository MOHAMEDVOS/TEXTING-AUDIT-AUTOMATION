"""Regression coverage for copying the first daily assignment only."""
import asyncio
from datetime import date
from types import SimpleNamespace

from dashboard import app as dashboard_app


class _FakeConnection:
    async def fetchval(self, query, *_args):
        assert "MAX(assigned_date)" in query
        return date(2026, 10, 19)

    async def fetch(self, query, *_args):
        assert "SELECT account_email FROM account_assignments" in query
        return [{"account_email": "b@example.com"}]


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, *_args):
        return False


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


def test_copy_latest_uses_first_texter_as_one_all_day_range(monkeypatch):
    captured = {}

    async def save_day(body, _request):
        captured["body"] = body
        return {"accounts": len(body.accounts), "periods": len(body.accounts)}

    async def source_segments(_source_date):
        return {"data": [
            {
                "account_email": "a@example.com",
                "segments": [
                    {"texter_name": "Alice", "start_time": "09:00", "end_time": "11:00"},
                    {"texter_name": "Bob", "start_time": "11:00", "end_time": "19:00"},
                ],
            },
            {
                "account_email": "b@example.com",
                "segments": [
                    {"texter_name": "Bob", "start_time": "00:00", "end_time": "24:00"},
                ],
            },
        ]}

    monkeypatch.setattr(dashboard_app.app.state, "pool", _FakePool(), raising=False)
    monkeypatch.setattr(dashboard_app, "api_save_day_segments", save_day)
    monkeypatch.setattr(dashboard_app, "api_assignment_day_segments", source_segments)

    result = asyncio.run(dashboard_app.api_copy_latest_assignments(
        SimpleNamespace(session={"user_email": "admin@example.com"}),
        date="2026-10-20",
    ))

    body = captured["body"]
    assert result["count"] == 1
    assert len(body.accounts) == 1
    assert body.accounts[0].account_email == "a@example.com"
    assert [segment.model_dump() for segment in body.accounts[0].segments] == [{
        "texter_name": "Alice",
        "start_time": "00:00",
        "end_time": "24:00",
    }]
    assert body.detect_shuffles is False
