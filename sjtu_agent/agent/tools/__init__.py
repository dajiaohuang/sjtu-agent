"""sjtu_agent/agent/tools — LLM tool definitions and implementations.

Organised by functional domain.  The public API is re-exported from _core.py,
which delegates the heavy work to domain-specific submodules (_reminders,
_email, etc.).  External callers never need to know about the internal split.
"""

from sjtu_agent.agent.tools import _core  # accessible as tools._core for tests
from sjtu_agent.agent.tools import _mcp_skills  # accessible for tests
from sjtu_agent.agent.tools._core import *  # noqa: F401, F403

# Underscore-prefixed names are part of the public API (imported by agent/__init__.py
# and chat_loop.py) but are not exported by wildcard import.
from sjtu_agent.agent.tools._core import (  # noqa: F401
    _fetch_ddls_parallel,
    _ddl_cache_get,
    _load_reminders,
    _is_interactive_chat_for_install_prompt,
    _detect_missing_parse_backend,
    _install_missing_backend_package,
)
