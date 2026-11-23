"""Shared dependencies for routes.

Routes ask for what they need; they never build it themselves.
"""

from fastapi import Request

from copilot.kb.store import KBReader
from copilot.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_kb(request: Request) -> KBReader:
    return get_services(request).kb


def get_masker(request: Request):
    return get_services(request).masker


def get_vault(request: Request):
    return get_services(request).vault


def get_tickets(request: Request):
    return get_services(request).tickets


def get_audit(request: Request):
    return get_services(request).audit
