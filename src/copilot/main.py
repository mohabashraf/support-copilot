"""Main entry point.

Run it with either:
    uv run copilot kb show
    uv run python -m copilot.main kb show
"""

from copilot.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
