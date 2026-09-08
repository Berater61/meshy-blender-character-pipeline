from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".cmd",
    ".example",
    ".gitignore",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {".git", ".venv", "Backups", "__pycache__"}
PATTERNS = {
    "Meshy-style API key": re.compile(r"msy" + r"_[A-Za-z0-9_-]{16,}", re.IGNORECASE),
    "literal bearer token": re.compile(
        r"Authorization\s*[:=].*Bearer\s+(?![{<$%])[A-Za-z0-9._-]{16,}",
        re.IGNORECASE,
    ),
}


def main() -> int:
    findings: list[tuple[Path, str]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 10_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append((path.relative_to(ROOT), label))

    if findings:
        print("Potential secrets detected:", file=sys.stderr)
        for path, label in findings:
            print(f"- {path}: {label}", file=sys.stderr)
        return 1

    print("Secret check passed: no key-like values detected in publishable files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
