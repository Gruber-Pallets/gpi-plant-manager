"""Preview or start one exact Plant Manager feedback item."""

from __future__ import annotations

from scripts import feedback_lifecycle


def main(argv: list[str] | None = None) -> int:
    return feedback_lifecycle.main(
        argv,
        command="start",
        prog="python -m scripts.process_feedback",
    )


if __name__ == "__main__":
    raise SystemExit(main())
