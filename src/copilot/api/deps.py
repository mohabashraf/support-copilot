"""Shared dependencies for routes.

Routes ask for what they need; they never build it themselves.
"""

from fastapi import Request

from copilot.kb.models import KnowledgeBase


def get_kb(request: Request) -> KnowledgeBase:
    return request.app.state.kb
