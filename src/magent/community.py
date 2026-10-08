"""Where users get help: the one mAgent community invite.

A leaf (no magent imports) so `magent --help` and `magent doctor` can import
it at top level. README.md and pyproject.toml's [project.urls] repeat the
literal; tests/unit/test_community.py pins every copy to this one.
"""

from __future__ import annotations

# Permanent (never expires), unlimited uses.
DISCORD_INVITE_URL = "https://discord.gg/P8g3pzBjDx"
