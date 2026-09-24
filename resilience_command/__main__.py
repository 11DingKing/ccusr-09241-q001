"""``python3 -m resilience_command`` 入口。"""

from .interfaces.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
