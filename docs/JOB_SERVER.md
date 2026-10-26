# Job Server destination

Added in Job Manager 2.6.0.

Release 2.6.0 publishes the plugin ZIP and requests the normal registry update.

MoleditPy Job Server is a LAN computational helper: an authenticated shared queue
running on a workstation/server. Job Manager remains the desktop client.

Only **cryptography >=50.0.2** is added as an optional dependency. Install it in
MoleditPy's Python environment with:

```sh
python -m pip install --upgrade "cryptography>=50.0.2"
```

Job Manager bundles the pure HTTP client. It does not require the Job Server
plugin, FastAPI, Uvicorn, psutil or a local server on the client machine. Existing
SSH/local/WSL destinations work when cryptography is absent. Selecting Job Server
without the optional package gives an installation hint; merely loading Job
Manager never imports it or starts a listener.

Open **Hosts**, select **Job Server (HTTPS)**, enter the server origin, and click
**Sign in / Register server device…**. Use the initial account password once;
subsequent connections authenticate with the saved device key. Registration fills
the server identity and selects its native Bash/PowerShell scheduler. Save the
host, then submit and monitor jobs through the ordinary Job Manager windows.
Resource budgets and queue administration belong to Job Server's dialogs/web UI;
Job Manager does not start another helper queue or apply SSH settings to it.

> [!WARNING]
> **Never expose Job Server to the public internet. Localhost, private LAN, or
> private Tailscale only, with trusted users.** Use HTTPS for LAN/Tailscale access;
> HTTP is accepted only on localhost. No Funnel, public reverse proxies/tunnels,
> or router port forwarding. Jobs share the service OS account, so admission is
> for trusted people rather than mutually untrusted tenants.

From 3.3.2, Job Server backends reuse authentication state in a bounded
process-local memory cache. Connections with different server identities,
credentials or certificate trust are isolated. Expiry and recent administrative
authentication rules still apply; changed/missing credentials or trust settings
invalidate reuse. The server continues to validate every request, and an HTTP
401 invalidates cached authentication without automatically replaying a command.
HTTP clients keep their own transports, and bearer tokens are not saved to disk.

TLS certificates are verified and redirects are refused. For a private certificate
authority, supply its CA certificate file in the host profile. Private keys are
kept outside the host/job JSON (DPAPI on Windows, private files on POSIX). Revoking
a device on the server invalidates its tokens; use a new key file to reenroll.

The server accepts uploaded input files, native commands, CPU/memory/walltime
requests and same-server dependencies. HPC modules/directives, existing remote
directories, relay inputs, force-run and scheduled start times are rejected.
Cancellation preserves the server's dependency rules; use **after-any** when a
dependent should continue after failure/cancellation.

The bundled client comes from the audited server commit recorded in
`job_manager/job_server_client/PROVENANCE.json`, under LGPL-3.0-or-later. Maintainers can
refresh it with `python scripts/sync_job_server_client.py /path/to/server-checkout`.
The script reads that exact Git commit and extracts only client modules/protocol
signing; it does not import a daemon or copy server authentication/database code.
