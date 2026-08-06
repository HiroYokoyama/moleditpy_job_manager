# Security Policy

## Security model

This plugin opens SSH connections on the user's behalf, so a few properties are
guaranteed by design and are covered by tests:

- **No credential is ever written to disk.** Host profiles store hostname, user,
  port, key *path* and backend only. A paramiko password lives in memory for the
  session and is requested again after a restart.
- **Unknown host keys are rejected, never auto-accepted.** The OpenSSH backend
  defers to the system client; the paramiko backend uses `RejectPolicy` and adds
  a key only after the user confirms the fingerprint shown to them.
- **Remote commands are constructed with `shlex.quote`**, and job names are
  reduced to `[A-Za-z0-9._-]` before they reach a remote path.
- **The OpenSSH backend runs in batch mode**, so it can never block on a hidden
  interactive prompt.

## Supported Versions

Only the latest release receives security updates.

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

To report a vulnerability, please contact the maintainers at titech.yoko.hiro[at]gmail.com.

Include the following in your report:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The version of the software affected
- Your operating system and environment details

If the issue is confirmed, a fix will be released as soon as possible.
