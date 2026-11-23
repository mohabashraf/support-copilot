"""LangSmith tracing (D6). Debugging only: the audit log is the legal record (PRD §6b).

The LangSmith SDK reads os.environ, not our .env, so `configure_tracing` copies the settings
into the process environment once at startup. A real environment variable still wins (same rule
as config.py). Nothing else is needed: LangGraph nodes and the LangChain `ChatAnthropic` calls
inside them are traced automatically, each model call nested under its node (instrumented once:
no extra SDK wrapper, which would create duplicate runs).

Privacy: PII is masked BEFORE the graph runs (pipeline/pii.py), so the root run's input and every
prompt are already masked. The placeholder → value mapping never enters the graph state.
"""

import os

from langchain_core.tracers.langchain import wait_for_all_tracers

from copilot.config import Settings
from copilot.log import get_logger

log = get_logger(__name__)

# Legacy destinations must not be combined with agent addressing (they could send traces to
# another project); removed from the process if present.
_LEGACY = ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT", "LANGCHAIN_SESSION", "LANGCHAIN_TRACING_V2")


def configure_tracing(st: Settings) -> bool:
    """Turn LangSmith tracing on or off for this process. Returns True if on."""
    if not st.langsmith_tracing or st.langsmith_api_key is None:
        os.environ["LANGSMITH_TRACING"] = "false"
        log.info(
            "tracing off", extra={"reason": "disabled" if not st.langsmith_tracing else "no key"}
        )
        return False
    for name in _LEGACY:
        os.environ.pop(name, None)
    values = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_ENDPOINT": st.langsmith_endpoint,
        "LANGSMITH_AGENT_ID": st.langsmith_agent_id,
        "LANGSMITH_AGENT_ENVIRONMENT": st.langsmith_agent_environment,
        "LANGSMITH_API_KEY": st.langsmith_api_key.get_secret_value(),
    }
    for name, value in values.items():
        os.environ.setdefault(name, value)
    log.info(
        "tracing on",
        extra={"agent": st.langsmith_agent_id, "environment": st.langsmith_agent_environment},
    )
    return True


def flush_traces() -> None:
    """Traces are sent in the background: CLI commands and scripts call this before exiting,
    or the last traces are lost."""
    wait_for_all_tracers()
