## What this adds

`magent link` pairs this machine with a magent link relay, so a phone or
another PC can see live session states, peek at a pane and upload a file
(pasted into a session) without a VPN or an open port. Everything the daemon
sends is end-to-end encrypted to the paired devices; the relay only routes.

- `magent link [--no-qr] [--add-device] [--remote-control/--no-remote-control]`
- `magent link --status` (add `--json` for scripts): the `Link` line is one of
  `unlinked`, `connecting`, `online`, `offline (retry in Ns)`,
  `offline (magent serve is not running)`,
  ``revoked (run `magent unlink`, then `magent link`)`` or
  `disabled (MAGENT_LINK=0)`
- `magent unlink`: revokes at the relay (best effort) and deletes the local
  link files
- new optional extra `link` (`cryptography`); `MAGENT_LINK=0` turns the
  feature off
- `magent status` and `magent doctor` show the link state

Remote control (send, choose, interrupt) stays off unless you answer yes, and
it is out of scope for this trial.

## Try it

```bash
uv tool install --force "magent-multi-ai-agents-manager[link,qr] @ git+https://github.com/DevinoSolutions/magent-multi-ai-agents-manager@feat/link"
magent link --help
```

`magent link --help` proves the branch is installed (`magent --version` prints
the release number either way). Restart `magent serve` so the link thread
loads, then link with the trial relay URL and the trial app URL the
maintainer gave you:

```powershell
$env:MAGENT_LINK_URL = "<the trial relay URL>"; $env:MAGENT_LINK_APP_URL = "<the trial app URL>"; magent link
```

```bash
MAGENT_LINK_URL="<the trial relay URL>" MAGENT_LINK_APP_URL="<the trial app URL>" magent link
```

The relay URL is saved with the link; the app URL is not, so
`magent link --add-device` needs `MAGENT_LINK_APP_URL` again.

1. Scan the QR with a signed-in phone (`Paired <device> (QR verified).`), or
   type the code on the pairing page and compare the 6-digit safety number
   before answering `y` to `Does the app show NNN NNN? [y/N]`.
2. Answer `n` to the remote-control question.
3. `magent link --status` says `online`. If it says
   `offline (magent serve is not running)`, start `magent serve`.
4. On the phone: live states, `peek`, upload a screenshot (pasted once).
5. Turn the network off for 2 minutes: within about a minute the status says
   `offline (retry in Ns)`, then `online` again within about a minute after
   reconnecting; a file uploaded while offline is pasted exactly once.
6. Unlink the host from the phone: `magent link --status` says
   ``revoked (run `magent unlink`, then `magent link`)``. `magent unlink`
   cleans up.

## Safety notes

- `~/.magent/link.json` holds the relay token and private keys; it is written
  owner-only. Never share it.
- Tokens and keys never appear in logs, argv, exceptions or crash reports.
- Without the `link` extra, `magent link` prints the install hint
  (`pip install "magent-multi-ai-agents-manager[link]"`) and exits 1; nothing
  else changes.
