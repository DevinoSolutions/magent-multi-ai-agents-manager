# Security Policy

## Supported Versions

magent follows semantic versioning. Security fixes land on the latest `1.0.x`
release line published to PyPI.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

**Please do not open a public issue for security problems.** Public disclosure
before a fix is available puts every user at risk.

Report privately through GitHub's Private Vulnerability Reporting:

1. Open the [**Security** tab](https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/security)
   of the repository.
2. Click **Report a vulnerability** to file a private advisory:
   <https://github.com/DevinoSolutions/magent-multi-ai-agents-manager/security/advisories/new>

If you are unable to use GitHub's reporting flow, email **amin@devino.ca**
instead.

We aim to acknowledge new reports within **5 business days** (best effort) and
will keep you updated as we investigate and prepare a fix. Please give us a
reasonable window to release a patch before any public disclosure.

## Scope

magent is a local-first CLI. Its one network surface is the optional mobile
image-upload server (`magent serve`, or `uploadServer: true` during launch).

**By design**, that server binds **only** to loopback and the host's Tailscale IP,
and it ships **no auth token** — the bind set *is* the access-control model.
Reaching it requires already being on your tailnet (or on the host itself).
Passing `magent serve --host 0.0.0.0` is an explicit, documented opt-out that
widens the bind to your LAN; that is a deliberate choice, not a default.

Because of this, **"the default upload server has no auth token" is working as
designed and is not a vulnerability.** In-scope reports include, for example:

- a way to reach the upload server outside its intended loopback + Tailscale bind;
- the default bind unexpectedly widening beyond loopback + Tailscale (i.e. without
  the explicit `--host` opt-out);
- any other path that lets untrusted input reach a project's agent session.

See the "Mobile image upload" and "Platform support" sections of the README for
the full networking model.
