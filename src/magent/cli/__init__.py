"""Registration hub for the magent CLI. `main` lives alone in app.py so
every command module can import it without a cycle (E6.md S2.1); importing
every command module below runs their `@main.command` decorators. The block
after that re-exports every symbol tests/external code still reach via
`magent.cli.<name>`.
"""

from __future__ import annotations

from magent.cli import (  # noqa: F401  # reason: side-effect import — runs each module's @main.command registration
    attach,
    attention_cmd,
    background,
    config_editor,
    config_io,
    docs,
    doctor,
    menu,
    mobile,
    session_picker,
    status,
    ui,
    watch,
)
from magent.cli.app import main
from magent.cli.attach import (
    _attach_flow,
    _default_attach_host,
    _split_target,
    _ssh_json,
    _tile_titles,
)
from magent.cli.background import _maybe_start_hotkey
from magent.cli.config_editor import _config_menu
from magent.cli.menu import _run_discovery, _show_menu
from magent.cli.session_picker import _run_sessions_picker
from magent.cli.status import _menu_down, _menu_status, _menu_up
from magent.cli.ui import _grouped, _print_session_overview

__all__ = [
    "_attach_flow",
    "_config_menu",
    "_default_attach_host",
    "_grouped",
    "_maybe_start_hotkey",
    "_menu_down",
    "_menu_status",
    "_menu_up",
    "_print_session_overview",
    "_run_discovery",
    "_run_sessions_picker",
    "_show_menu",
    "_split_target",
    "_ssh_json",
    "_tile_titles",
    "main",
]
