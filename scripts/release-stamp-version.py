#!/usr/bin/env python3
"""Stamp the PEP 440 release version into pyproject.toml + __init__.py.

Invoked by .github/workflows/release.yml immediately before
`python -m build`. Takes a git tag name as its only argument, normalizes
it to a PEP 440-compliant version string, and rewrites the version
declarations so the built wheel matches the tag.

Tag formats accepted:
    v0.10.0            -> 0.10.0
    v0.10.0.dev1       -> 0.10.0.dev1
    v0.10.0-dev.1      -> 0.10.0.dev1    (normalization)
    v0.10.0.rc1        -> 0.10.0rc1      (PEP 440 dedotting)
    v0.10.0-alpha.2    -> 0.10.0a2
    v0.10.0-beta.3     -> 0.10.0b3
    v0.10.0.post1      -> 0.10.0.post1

Tags that don't match fail the script so malformed tags never become
malformed releases.
"""

from __future__ import annotations

import pathlib
import re
import sys


TAG_PATTERN = re.compile(
    r"^(?P<base>\d+\.\d+\.\d+)"
    r"(?:[-.]"
    r"(?P<kind>dev|rc|a|alpha|b|beta|post)"
    r"\.?(?P<n>\d+)"
    r")?$"
)

# alpha/beta canonicalize to single letters; dev/rc/post keep their spelling
KIND_CANONICAL = {"alpha": "a", "beta": "b"}


def normalize_tag(raw: str) -> str:
    stripped = raw.removeprefix("v")
    match = TAG_PATTERN.match(stripped)
    if match is None:
        raise SystemExit(
            f"::error::Tag {raw!r} is not PEP 440 compatible. "
            f"Expected vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH(.|-)KIND(.|)N."
        )
    base = match.group("base")
    kind = match.group("kind")
    if kind is None:
        return base
    n = match.group("n")
    canonical = KIND_CANONICAL.get(kind, kind)
    # PEP 440 uses `.devN`, `.postN`; but `rcN`, `aN`, `bN` (no dot).
    sep = "." if canonical in {"dev", "post"} else ""
    return f"{base}{sep}{canonical}{n}"


def stamp_file(path: pathlib.Path, pattern: re.Pattern[str], replacement: str) -> None:
    text = path.read_text()
    new_text, count = pattern.subn(replacement, text, count=1)
    if count == 0:
        raise SystemExit(f"::error::{path}: no line matched pattern {pattern.pattern!r}")
    path.write_text(new_text)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <tag-name>", file=sys.stderr)
        return 2
    raw_tag = argv[1]
    version = normalize_tag(raw_tag)
    print(f"Raw tag:         {raw_tag}")
    print(f"PEP 440 version: {version}")

    repo_root = pathlib.Path(__file__).resolve().parent.parent

    stamp_file(
        repo_root / "pyproject.toml",
        re.compile(r'(?m)^version = ".*"$'),
        f'version = "{version}"',
    )
    stamp_file(
        repo_root / "hermes_cli" / "__init__.py",
        re.compile(r'(?m)^__version__ = ".*"$'),
        f'__version__ = "{version}"',
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
