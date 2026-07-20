"""Failure fingerprints.

A fingerprint identifies *which* failure occurred, independent of run-to-run
noise (addresses, temp paths, large numbers). Two runs with the same
fingerprint failed "the same way"; a perturbation that produces a *different*
fingerprint than the baseline broke the test for an unrelated reason and must
never earn causal credit for the flake.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]{4,}")
_TMP_PATH = re.compile(
    r"(?:/private)?/(?:var/folders|tmp)/[^\s'\"]+"
    r"|[A-Za-z]:\\Users\\[^\s\\]+\\AppData\\Local\\Temp\\[^\s'\"]+"
)
_LONG_NUM = re.compile(r"\d{5,}")
_WS = re.compile(r"\s+")


def normalize_message(message: str) -> str:
    """First line of an exception message with volatile tokens masked."""
    stripped = message.strip()
    if not stripped:
        return ""
    first = stripped.splitlines()[0]
    first = _HEX_ADDR.sub("0xADDR", first)
    first = _TMP_PATH.sub("<tmpdir>", first)
    first = _LONG_NUM.sub("<n>", first)
    first = _WS.sub(" ", first).strip()
    return first[:200]


@dataclass(frozen=True)
class Fingerprint:
    phase: str  # "setup" | "call" | "teardown" | "run" (whole-process outcomes)
    exc_type: str  # "AssertionError" or "pkg.mod.Error"; "<hang>"/"<crash>" for process outcomes
    message: str  # normalized first line — display only, NOT part of the identity
    crash_site: str  # "path/to/file.py:123", best effort; "" for process outcomes

    def key(self) -> tuple[str, str, str]:
        # The message is deliberately excluded: pytest's assertion rewriting
        # embeds run-varying values ("assert 'banana' not in ..."), so matching
        # on it would misclassify nearly every real flake as a different
        # failure. Exception type + crash site is the stable identity.
        return (self.phase, self.exc_type, self.crash_site)

    def digest(self) -> str:
        return hashlib.sha256("\x1f".join(self.key()).encode("utf-8")).hexdigest()[:12]

    def describe(self) -> str:
        where = f" at {self.crash_site}" if self.crash_site else ""
        msg = f": {self.message}" if self.message else ""
        return f"{self.exc_type}{where} during {self.phase}{msg}"


HANG = Fingerprint(phase="run", exc_type="<hang>", message="", crash_site="")
CRASH = Fingerprint(phase="run", exc_type="<crash>", message="", crash_site="")
