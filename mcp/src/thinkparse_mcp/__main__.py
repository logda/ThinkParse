"""Entry point: python -m thinkparse_mcp."""

from __future__ import annotations

import sys

from .config import ConfigError, Settings


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    from .server import run

    run(settings)


if __name__ == "__main__":
    main()
