"""
Configuration settings for SmarterContact Audit Automation.
Loads from .env file and provides defaults.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)
import pytz
from datetime import datetime



# ─── Browser Automation ─────────────────────────────────────
SMARTERCONTACT_LOGIN_URL = "https://app.smartercontact.com/login"
SMARTERCONTACT_REPORTING_URL = "https://app.smartercontact.com/reporting"
SMARTERCONTACT_MESSENGER_URL = "https://app.smartercontact.com/messenger"

MAX_PARALLEL_WORKERS = int(os.getenv("MAX_PARALLEL_WORKERS", "5"))
HEADLESS_MODE = os.getenv("HEADLESS_MODE", "true").lower() == "true"
SCREENSHOT_ON_ERROR = os.getenv("SCREENSHOT_ON_ERROR", "false").lower() == "true"

# Date filter applied to the inbox before extracting conversations.
# Options: "today" | "last_week" | "this_month" | "last_month" | "last_30_days" | "last_year" | "all_time"
DATE_FILTER = os.getenv("DATE_FILTER", "today")
DEFAULT_SAMPLE_SIZE = int(os.getenv("DEFAULT_SAMPLE_SIZE", "10"))

# ─── Anti-Detection ─────────────────────────────────────────
MIN_DELAY = float(os.getenv("MIN_DELAY_SECONDS", "2"))
MAX_DELAY = float(os.getenv("MAX_DELAY_SECONDS", "5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Viewport sizes to rotate between (looks like different devices)
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
]

# User agents to rotate
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# ─── Database ────────────────────────────────────────────────
_raw_db_url = os.getenv("DATABASE_URL")
if not _raw_db_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Define it in .env (local) or as a "
        "Railway service variable (production). No hardcoded fallback."
    )
DATABASE_URL = _raw_db_url
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ─── Logging ─────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = PROJECT_ROOT / os.getenv("LOG_DIR", "logs")

# ─── Groq AI (primary) ──────────────────────────────────────
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ─── Ollama local model (fallback / offline use) ─────────────
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

# ─── Credentials ────────────────────────────────────────────
CREDENTIALS_KEY = os.getenv("CREDENTIALS_KEY", "")

# ─── Dream Worker (Self-Learning) ───────────────────────────
DREAM_WORKER_MIN_HOURS    = int(os.getenv("DREAM_WORKER_MIN_HOURS",    "4"))
DREAM_WORKER_MIN_SESSIONS = int(os.getenv("DREAM_WORKER_MIN_SESSIONS", "3"))
DREAM_WORKER_MAX_RULES    = int(os.getenv("DREAM_WORKER_MAX_RULES",    "5"))
LEARNED_RULES_PATH        = PROJECT_ROOT / "ai" / "learned_rules.json"
DREAM_STATE_PATH          = PROJECT_ROOT / "ai" / "dream_state.json"

# ─── ML Pre-Filter Pipeline ─────────────────────────────────
# Master switch. When False, prefilter never runs and analyzer behavior is
# identical to the pre-prefilter codebase.
PREFILTER_ENABLED       = os.getenv("PREFILTER_ENABLED", "true").lower() == "true"

# Shadow mode: prefilter runs and records decisions to prefilter_decisions,
# but the result is DISCARDED — Groq still produces the final score.
# Use this to evaluate accuracy before flipping any tier live.
PREFILTER_SHADOW_MODE   = os.getenv("PREFILTER_SHADOW_MODE", "false").lower() == "true"

# Per-tier live switches (only honored when PREFILTER_SHADOW_MODE=False).
# Conservative default: only Tier 1 short-circuits live initially.
PREFILTER_T1_LIVE       = os.getenv("PREFILTER_T1_LIVE", "true").lower() == "true"
PREFILTER_T2_LIVE       = os.getenv("PREFILTER_T2_LIVE", "false").lower() == "true"
PREFILTER_T3_LIVE       = os.getenv("PREFILTER_T3_LIVE", "false").lower() == "true"
PREFILTER_T4_LIVE       = os.getenv("PREFILTER_T4_LIVE", "true").lower() == "true"

# Tier 2 confidence: cosine-similarity threshold + min cluster size.
PREFILTER_T2_SIM_THRESHOLD = float(os.getenv("PREFILTER_T2_SIM_THRESHOLD", "0.85"))
PREFILTER_T2_MIN_NEIGHBORS = int(os.getenv("PREFILTER_T2_MIN_NEIGHBORS", "3"))

# Tier 3 confidence: max flag-probability + min predicted score across all 4 dims.
# Calibrated 2026-05-07 on 500-conv test: clean ~0.30, flagged ~0.63 → 0.35 = safe gap.
PREFILTER_T3_MAX_FLAG_PROB = float(os.getenv("PREFILTER_T3_MAX_FLAG_PROB", "0.35"))
PREFILTER_T3_MIN_SCORE     = float(os.getenv("PREFILTER_T3_MIN_SCORE",     "75"))
PREFILTER_T3_LABEL_CONFIDENCE = float(os.getenv("PREFILTER_T3_LABEL_CONFIDENCE", "0.7"))

# Embedding model (sentence-transformers, downloaded on first use).
PREFILTER_EMBEDDING_MODEL = os.getenv("PREFILTER_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# Persistent embedding service (Solution B). When set, embedder.py fetches
# vectors over HTTP from a long-lived process (the dashboard) instead of
# loading the ~80MB sentence-transformer model in every audit subprocess.
# Empty → load the model in-process (CLI runs, or when no service is up).
# The dashboard sets this in the env of every subprocess it spawns.
EMBEDDING_SERVICE_URL = os.getenv("EMBEDDING_SERVICE_URL", "")

# Artifact paths (kNN index + classifier weights).
PREFILTER_DIR             = PROJECT_ROOT / "ai" / "prefilter" / "artifacts"
PREFILTER_INDEX_PATH      = PREFILTER_DIR / "knn_index.faiss"
PREFILTER_INDEX_META_PATH = PREFILTER_DIR / "knn_index_meta.json"
PREFILTER_CLASSIFIER_PATH = PREFILTER_DIR / "classifier.joblib"

# Pre-flight flag-trigger routing. When True, conversations whose text
# contains any flag-trigger pattern (opt-out, profanity, $ amounts, etc.)
# bypass all ML tiers and go straight to Groq for a full audit.
PREFILTER_FLAG_ROUTING_ENABLED = os.getenv("PREFILTER_FLAG_ROUTING_ENABLED", "true").lower() == "true"

# Validation-aware index builder. When True, index_builder ONLY includes
# conversations whose validation_log.status = 'valid'. Keep False until
# ~50 manager validations exist; flipping it on with zero validations
# empties the index.
PREFILTER_REQUIRE_VALIDATION = os.getenv("PREFILTER_REQUIRE_VALIDATION", "false").lower() == "true"

# ─── Semantic Auto-Learning ─────────────────────────────────
SEMANTIC_LEARNING_ENABLED = os.getenv("SEMANTIC_LEARNING_ENABLED", "true").lower() == "true"
SEMANTIC_MIN_SCORE        = float(os.getenv("SEMANTIC_MIN_SCORE",        "88.0"))
SEMANTIC_MAX_SIMILARITY   = float(os.getenv("SEMANTIC_MAX_SIMILARITY",   "0.75"))
SEMANTIC_MIN_PROMOTE      = int(os.getenv("SEMANTIC_MIN_PROMOTE",        "5"))
SEMANTIC_MAX_PER_RUN      = int(os.getenv("SEMANTIC_MAX_PER_RUN",        "50"))
SEMANTIC_REBUILD_TIMEOUT  = int(os.getenv("SEMANTIC_REBUILD_TIMEOUT",    "300"))
SEMANTIC_TRAIN_TIMEOUT    = int(os.getenv("SEMANTIC_TRAIN_TIMEOUT",      "600"))

# ─── Timezone ───────────────────────────────────────────────
TIMEZONE_STR = os.getenv("TZ", "America/New_York")
TIMEZONE = pytz.timezone(TIMEZONE_STR)

def get_now() -> datetime:
    """Get current time in the configured timezone."""
    return datetime.now(TIMEZONE)


# ─── Google OAuth ─────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
APP_BASE_URL         = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")

# ─── Session Cookie ───────────────────────────────────────────────────────────
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "")
if not SESSION_SECRET_KEY:
    import secrets as _sec
    SESSION_SECRET_KEY = _sec.token_hex(32)
    import logging as _log
    _log.getLogger(__name__).warning(
        "SESSION_SECRET_KEY not set — using ephemeral random key. "
        "Sessions will be lost on every server restart. "
        "Set SESSION_SECRET_KEY in .env or Railway environment variables."
    )

# ─── Tool Access Bootstrap ────────────────────────────────────────────────────
TOOL_ACCESS_SEED_EMAILS: list[str] = [
    e.strip().lower()
    for e in os.getenv("TOOL_ACCESS_SEED_EMAILS", "").split(",")
    if e.strip()
]


