"""Shared helpers for the subprocess-driven end-to-end tests.

These tests run child ``pytest`` processes to exercise the plugin for real. How
the plugin gets into those children depends on how flakedoctor is available:

* **Installed** (CI, and every real user): pytest auto-loads it via the
  ``pytest11`` entry point. Passing ``-p flakedoctor._plugin`` *on top of that*
  registers the same module under a second name, and pytest aborts with
  "Plugin already registered under a different name". So we must NOT force it.
* **Bare source tree** (local ``PYTHONPATH=src``, not installed): there is no
  entry point, so the child must be told to load the plugin with ``-p``.

``plugin_load_args()`` returns whichever is correct for the current environment,
so the suite passes both when installed and when run straight from ``src``.
"""

from __future__ import annotations

import sys
from importlib.metadata import entry_points


def _plugin_autoloads() -> bool:
    """True when an installed flakedoctor will auto-load its plugin via the
    ``pytest11`` entry point (so forcing it with ``-p`` would double-register)."""
    return any(ep.name == "flakedoctor" for ep in entry_points(group="pytest11"))


def plugin_load_args() -> list[str]:
    """``["-p", "flakedoctor._plugin"]`` only when the entry point won't."""
    return [] if _plugin_autoloads() else ["-p", "flakedoctor._plugin"]


def pytest_argv(*args: str) -> list[str]:
    """A ``python -m pytest ...`` argv that loads the plugin exactly once."""
    return [sys.executable, "-m", "pytest", *plugin_load_args(), *args]
