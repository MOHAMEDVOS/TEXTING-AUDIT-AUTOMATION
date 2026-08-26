"""Shared pytest configuration.

The project reads required settings (DATABASE_URL, FIREBASE_API_KEY) at import
time, so tests need them present before any project module is imported. Real
values are never used — nothing in this suite opens a connection.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("FIREBASE_API_KEY", "test-key-not-real")
os.environ.setdefault("TZ", "America/New_York")
