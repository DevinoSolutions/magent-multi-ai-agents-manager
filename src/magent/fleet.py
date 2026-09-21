"""Fleet control: talk to one running agent by name, and read/switch its model.

A leaf over :mod:`magent.psmux` (which is itself a leaf over :mod:`magent.log`)
-- no dependency on the cli package. The commands ``magent send`` / ``magent
model`` / ``magent peek`` and ``magent sessions --json`` are thin shells over
the functions here; this module owns the parsing and the psmux choreography so
all of it is unit-testable without a terminal.

Two ideas do the work:

* **Reading the footer.** Claude Code paints ``<Model> * <effort>`` (a MIDDLE
  DOT separator) at the bottom of the pane and a spinner / dialog / limit
  notice above it. :func:`parse_footer` and :func:`classify_state` turn the
  captured pane text into ``(model, effort, state)`` so a caller knows whether
  a session is idle, mid-turn, waiting on a prompt, or out of headroom.

* **Typing, safely.** :func:`paste_and_enter` pastes text with ``send-keys
  -l`` (literal) then a separate ``Enter``. Because every psmux call is a list
  argv handed straight to the process (never a shell), a prompt or a
  ``/model`` command reaches the agent verbatim -- Git Bash / MSYS never sees a
  leading ``/`` to rewrite into ``C:/Program Files/Git/...``.
"""

from __future__ import annotations

import re
import time

from magent import psmux

# --- Footer / state parsing -------------------------------------------------

# The footer separator is a MIDDLE DOT, U+00B7. We spell it as an escape so
# this source file stays pure ASCII; we only ever READ it (the ASCII-only rule
# is about status bars magent renders, not about the agent's own UI we parse).
_MIDDOT = "\u00b7"
# U+276F, which Claude Code draws in two places: beside a numbered menu option
# (a dialog -- see ``_DIALOG_RE``) and at the head of the INPUT line (see
# ``input_line``). Spelled as an escape for the same reason as ``_MIDDOT``.
_CARET = "\u276f"

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "auto")

# "<Model> * <effort>", e.g. "Fable 5.1 * high". The model group is non-greedy
# and the effort token is a hard anchor, so it captures the whole model name
# ("Fable 5.1", "Opus 5", "Sonnet 4.5") without swallowing text to its left.
_FOOTER_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9.\- ]*?)\s*"
    + re.escape(_MIDDOT)
    + r"\s*("
    + "|".join(EFFORTS)
    + r")\b"
)

# The agent is mid-turn: a running spinner timer "(12s" / "(3m", the interrupt
# hint, queued messages, or an in-progress /compact.
_BUSY_RE = re.compile(
    r"esc to interrupt|\(\d+[ms]\b|queued message|ompacting", re.IGNORECASE
)
# The agent is waiting on a yes/no or a numbered menu choice.
_DIALOG_RE = re.compile(
    r"do you want|\(y\)es|esc to cancel|press enter to|" + re.escape(_CARET) + r"\s*\d",
    re.IGNORECASE,
)
# The account is out of headroom and cannot proceed until a window resets.
_LIMIT_RE = re.compile(
    r"usage limit|rate limit|limit reached|limit will reset|approaching (?:your )?usage",
    re.IGNORECASE,
)

# Model CLI alias -> the display-name prefix its footer shows. Used only to
# VERIFY a `/model` switch landed; an unknown alias verifies softly (effort
# only) rather than reporting a false failure.
_MODEL_DISPLAY: dict[str, str] = {
    "fable": "Fable",
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
}

_LITERAL_SEND_TIMEOUT_S = 30.0
_ENTER_SETTLE_S = 0.3
_SWITCH_SETTLE_S = 2.5
# How long a just-pasted command gets to take visible effect before an "idle"
# reading is believed. See ``wait_for_idle``'s ``settle``.
COMMAND_SETTLE_S = 2.0
# Below this many characters a prompt is too short to reliably tell "still
# sitting unsent in the input line" from "echoed in the agent's reply", so
# send-verification passes rather than risk a false "not confirmed".
_VERIFY_MIN_CHARS = 6


def parse_footer(pane: str) -> tuple[str | None, str | None]:
    """Return ``(model, effort)`` read from the pane footer, or ``(None, None)``.

    Takes the LAST footer match in the capture: the live footer is the bottom
    line, and older footers can scroll by in the transcript above it.
    """
    matches = list(_FOOTER_RE.finditer(pane or ""))
    if not matches:
        return None, None
    m = matches[-1]
    return m.group(1).strip(), m.group(2)


def classify_state(pane: str) -> str:
    """Classify a captured pane: dialog / busy / limit / idle / nopane.

    Precedence is dialog > busy > limit > idle: a session waiting on the user
    is the most actionable signal, an actively-working session outranks a
    stale limit notice in its scrollback, and only a pane that is none of those
    (and not empty) is safe to type into.
    """
    if not (pane or "").strip():
        return "nopane"
    if _DIALOG_RE.search(pane):
        return "dialog"
    if _BUSY_RE.search(pane):
        return "busy"
    if _LIMIT_RE.search(pane):
        return "limit"
    return "idle"


def read_state(name: str, *, psmux_bin: str | None = None) -> dict[str, object]:
    """Capture ``name``'s pane once and return ``{state, model, effort}``."""
    pane = psmux.capture_pane(name, psmux=psmux_bin)
    model, effort = parse_footer(pane)
    return {"state": classify_state(pane), "model": model, "effort": effort}


# --- Name resolution --------------------------------------------------------


def resolve_session(query: str, names: list[str]) -> str | None:
    """Resolve ``query`` to one of ``names``: exact (case-insensitive) first,
    then a UNIQUE substring, then a UNIQUE prefix. Ambiguous or absent -> None.
    """
    if not query:
        return None
    q = query.lower()
    for n in names:
        if n.lower() == q:
            return n
    subs = [n for n in names if q in n.lower()]
    if len(subs) == 1:
        return subs[0]
    prefix = [n for n in names if n.lower().startswith(q)]
    if len(prefix) == 1:
        return prefix[0]
    return None


# --- Typing into a pane -----------------------------------------------------


def flatten(text: str) -> str:
    """Collapse internal newlines to single spaces.

    A lone Enter SUBMITS in Claude Code, so a literal newline inside a pasted
    prompt would fire the prompt early, one line at a time. Flattening keeps
    the whole prompt on one input line; the single trailing Enter submits it.
    """
    return re.sub(r"\s*\n\s*", " ", text.strip())


def paste_and_enter(
    name: str,
    text: str,
    *,
    psmux_bin: str | None = None,
    timeout: float = _LITERAL_SEND_TIMEOUT_S,
    settle: float = _ENTER_SETTLE_S,
) -> bool:
    """Paste ``text`` literally into ``name``'s input line, then press Enter.

    Returns True only if both the literal paste and the Enter succeeded. The
    paste and the Enter are two separate psmux calls on purpose: ``-l`` sends
    the text verbatim (so ``/model`` stays ``/model``), and the Enter is a real
    key name, not part of the literal payload.
    """
    flat = flatten(text)
    if not flat:
        return False
    if not psmux.send_keys(
        name, flat, target=name, literal=True, psmux=psmux_bin, timeout=timeout
    ):
        return False
    time.sleep(settle)
    return psmux.send_keys(name, "Enter", target=name, psmux=psmux_bin)


def input_line(pane: str) -> str | None:
    """The pane's INPUT line: the last line whose text starts with the caret.

    Claude Code's input line is not the bottom of the pane -- below it sit a
    rule, the model/effort footer, and a permissions/hints row. ``None`` means
    no caret line was found at all, which is the only case where the pane's
    last line is the best available guess.
    """
    for line in reversed((pane or "").rstrip().splitlines()):
        if line.strip().startswith(_CARET):
            return line
    return None


def looks_unsent(pane: str, text: str) -> bool:
    """True if the prompt appears to be STILL sitting in the input line.

    Heuristic used to confirm the Enter actually submitted: if the head of the
    (whitespace-collapsed) prompt is still on the INPUT line, it was not sent.
    Very short prompts are unverifiable and always report "sent".

    The input line is found by its caret, NOT taken as the pane's last line,
    and that distinction is the whole check. Captured from a real Claude Code
    pane, the bottom four rows are a rule, the caret line, a rule, the footer
    and the hints row -- so an unsent prompt sits FOUR lines above the bottom
    and a last-line test could never see it. Measured that way: exit code 4
    ("send not confirmed") was unreachable in practice against the real agent.
    Panes with no caret at all (a bare shell, an agent mid-boot) keep the
    last-line behaviour, which is the only signal there is.
    """
    flat = re.sub(r"\s+", " ", text.strip())
    head = flat[:25]
    if len(head) < _VERIFY_MIN_CHARS:
        return False
    line = input_line(pane)
    if line is None:
        line = ((pane or "").rstrip().splitlines() or [""])[-1]
    return head in line


def wait_for_idle(
    name: str,
    *,
    psmux_bin: str | None = None,
    deadline: float,
    poll: float = 2.0,
    settle: float = 0.0,
) -> bool:
    """Poll until ``name``'s pane classifies as idle, or ``deadline`` passes.

    ``deadline`` is an absolute ``time.monotonic()`` value. Returns True the
    moment the session is idle; on timeout it takes one final reading so a
    session that just went idle is not reported busy by a stale poll.

    ``settle`` postpones the FIRST reading, and exists because "idle" is
    ambiguous right after a command was pasted: the agent has not reacted yet,
    so the pane still looks exactly like an idle one. Callers that just SENT
    the thing they are now waiting out (``send --compact``) must not believe
    that reading -- caught against a real multiplexer, where ``/compact``'s
    idle answer came back instantly, the follow-up prompt was pasted into a
    session that was about to start compacting, and it sat echoed-but-unsent in
    the input line until the turn ended: a false exit 4 on a prompt that was in
    fact queued. Callers that are only ASKING (``--wait-idle``) leave it at 0,
    where an immediate idle answer is both correct and the point.
    """
    if settle > 0:
        time.sleep(settle)
    while time.monotonic() < deadline:
        if classify_state(psmux.capture_pane(name, psmux=psmux_bin)) == "idle":
            return True
        time.sleep(poll)
    return classify_state(psmux.capture_pane(name, psmux=psmux_bin)) == "idle"


# --- Model / effort switching ----------------------------------------------


def switch_model(
    name: str,
    model: str,
    effort: str | None = None,
    *,
    psmux_bin: str | None = None,
    settle: float = _SWITCH_SETTLE_S,
) -> bool:
    """Send ``/model <model>`` then, if given, ``/effort <effort>``.

    Both commands are built here in Python and pasted literally, so the leading
    slash is never handed to a shell. Returns True only if every command was
    delivered; the caller re-reads the footer to confirm it took effect.
    """
    commands = [f"/model {model}"]
    if effort:
        commands.append(f"/effort {effort}")
    for command in commands:
        if not paste_and_enter(name, command, psmux_bin=psmux_bin):
            return False
        time.sleep(settle)
    return True


def verify_switch(pane: str, model: str, effort: str | None = None) -> bool:
    """True if the pane footer now reflects ``model`` (and ``effort`` if given).

    An unknown model alias cannot be mapped to its display name, so it verifies
    on effort alone rather than failing a switch that actually landed.
    """
    footer_model, footer_effort = parse_footer(pane)
    if not footer_model:
        return False
    want = _MODEL_DISPLAY.get(model.lower())
    model_ok = (want.lower() in footer_model.lower()) if want else True
    effort_ok = effort is None or footer_effort == effort
    return model_ok and effort_ok
