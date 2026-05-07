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
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/texting_audit")

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
PREFILTER_SHADOW_MODE   = os.getenv("PREFILTER_SHADOW_MODE", "true").lower() == "true"

# Per-tier live switches (only honored when PREFILTER_SHADOW_MODE=False).
# Conservative default: only Tier 1 short-circuits live initially.
PREFILTER_T1_LIVE       = os.getenv("PREFILTER_T1_LIVE", "true").lower() == "true"
PREFILTER_T2_LIVE       = os.getenv("PREFILTER_T2_LIVE", "false").lower() == "true"
PREFILTER_T3_LIVE       = os.getenv("PREFILTER_T3_LIVE", "false").lower() == "true"

# Tier 2 confidence: cosine-similarity threshold + min cluster size.
PREFILTER_T2_SIM_THRESHOLD = float(os.getenv("PREFILTER_T2_SIM_THRESHOLD", "0.92"))
PREFILTER_T2_MIN_NEIGHBORS = int(os.getenv("PREFILTER_T2_MIN_NEIGHBORS", "3"))

# Tier 3 confidence: max flag-probability + min predicted score across all 4 dims.
PREFILTER_T3_MAX_FLAG_PROB = float(os.getenv("PREFILTER_T3_MAX_FLAG_PROB", "0.15"))
PREFILTER_T3_MIN_SCORE     = float(os.getenv("PREFILTER_T3_MIN_SCORE",     "75"))

# Embedding model (sentence-transformers, downloaded on first use).
PREFILTER_EMBEDDING_MODEL = os.getenv("PREFILTER_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

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
