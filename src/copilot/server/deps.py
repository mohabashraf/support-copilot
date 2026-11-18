"""Shared dependencies for routes.

Routes ask for what they need; they never build it themselves.
"""

from fastapi import Request

from copilot.kb.store import KBReader
from copilot.retrieval.search import Retriever
from copilot.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_kb(request: Request) -> KBReader:
    return get_services(request).kb


def get_retriever(request: Request) -> Retriever:
    return get_services(request).retriever
