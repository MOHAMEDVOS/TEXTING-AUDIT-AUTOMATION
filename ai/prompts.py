# -*- coding: utf-8 -*-
"""
Prompt version tag.

The Groq system-prompt builder (SYSTEM_PROMPT, get_system_prompt, and the
funnel-tier/account-guidelines assembly around it) was removed when Groq was
decommissioned in favor of the ML-only pipeline. PROMPT_VERSION is kept —
it's stamped on every conversation_scores / flag_feedback row to tie results
back to the rule-set revision that produced them.
"""

# Bump this whenever the scoring rules / RED_FLAGS / funnel logic change.
# Stored on every conversation_scores row (prompt_version) so feedback and
# audits can be tied back to the exact rule set that produced them.
# Format: YYYY-MM-DD.N  (N = revision within the day)
PROMPT_VERSION = "2026-06-24.1"
