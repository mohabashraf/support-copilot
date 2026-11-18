---
name: add-setting
description: Add, rename or remove a configuration setting (env variable) in the support co-pilot, the .env-first way. Use whenever code needs a new configurable value (model name, threshold, URL, limit, flag) instead of hard-coding it.
---

# Adding a setting

`.env` is the single source of truth. `config.py` declares the type and has **no defaults**, so a
missing value stops startup with a clear message instead of silently using a guess.

## Steps

1. **`.env.example`** (committed template): add the variable with a safe example value and a comment
   saying what it is and what values are allowed. Never put a real secret here.
   ```bash
   # Abstain when the best retrieval score is below this (from `copilot eval retrieval`)
   ABSTAIN_THRESHOLD=0.74
   ```

2. **`src/copilot/config.py`**: add the field to `Settings`, typed, **no default**. Use `Literal[...]`
   for a fixed set of values and a `field_validator` only for real normalisation (see `log_file`).
   ```python
   abstain_threshold: float
   ```
   Secrets use `SecretStr` so they never print in logs or errors.

3. **Your own `.env`** (not committed): add the same line. `uv run pytest` will fail until you do if
   the code reads it.

4. **Docker**: nothing to do if the value is the same in a container. If it must differ (hosts, URLs,
   paths), override it in the `x-copilot.environment` block of `docker-compose.yml`, with a comment.

5. **Read it** where needed through `get_settings()` (cached once per process), and pass the value
   or the `Settings` object down. Never call `os.getenv` directly.

6. **Test**: `tests/test_config.py::test_env_example_has_every_setting` checks that `.env.example`
   is complete. Add a test for any validation you wrote (invalid value fails, edge cases).

7. If the setting reflects a design choice (a model, a threshold), record why in `docs/DECISIONS.md`.

## Removing or renaming
Remove/rename it in all three places (`.env.example`, `config.py`, `.env`) plus `docker-compose.yml`
if overridden there. `extra="ignore"` means a leftover line in someone's `.env` is harmless.
