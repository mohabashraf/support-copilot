"""Test API keys: one per role, plus helpers to build auth headers."""

from copilot.server.auth import Principal, hash_key

HELPDESK_KEY, SARA_KEY, OMAR_KEY = "sk_test_helpdesk", "sk_test_sara", "sk_test_omar"
KEYS = {
    hash_key(HELPDESK_KEY): Principal("helpdesk", "helpdesk"),
    hash_key(SARA_KEY): Principal("sara", "agent"),
    hash_key(OMAR_KEY): Principal("omar", "agent"),
}
HELPDESK = {"Authorization": f"Bearer {HELPDESK_KEY}"}
SARA = {"Authorization": f"Bearer {SARA_KEY}"}
OMAR = {"Authorization": f"Bearer {OMAR_KEY}"}
