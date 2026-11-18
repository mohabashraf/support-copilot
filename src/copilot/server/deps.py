"""Shared dependencies for routes.

Routes ask for what they need; they never build it themselves. Everything is read from ONE
`app.state.services` object per request, so a hot reload (KBWatcher) can never mix an old KB
with a new retriever inside a request.
"""

from fastapi import Request

from copilot.kb.models import KnowledgeBase
from copilot.retrieval.search import Retriever
from copilot.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_kb(request: Request) -> KnowledgeBase:
    return get_services(request).kb


def get_retriever(request: Request) -> Retriever:
    return get_services(request).retriever
