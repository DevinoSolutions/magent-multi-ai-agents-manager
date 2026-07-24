"""Shared click.style shortcut, hoisted out of cli.py and launch.py where it
was independently defined twice (LS-A-003, duplication). Call sites use
`style` directly; the repo-wide S -> style rename (E10) retired the earlier
transitional `S` alias.
"""

from __future__ import annotations

import click

style = click.style
