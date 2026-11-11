# Job Server destination

Added in Job Manager 2.6.0.

Release 2.6.0 publishes the plugin ZIP and requests the normal registry update.

[MoleditPy Job Server](https://github.com/HiroYokoyama/moleditpy_job_server) is a LAN computational helper: an authenticated shared queue
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

Open **Hosts**, select **Job Server (HTTPS)**, enter the server HTTPS URL, and click
**Sign in to Job Server…**. Use the initial account password once;
subsequent connections authenticate with the saved device key. Registration fills
the server identity and selects its native Bash/PowerShell scheduler. The grayed
credential path and identity fields are managed automatically; do not enter them
manually. Save the
host, then submit and monitor jobs through the ordinary Job Manager windows.
Submit checks the server's total CPU and memory budgets before creating jobs.
If a request exceeds either budget, the error stays in Submit with the inputs
intact so you can adjust Resources and retry. A busy server with sufficient total
capacity can still accept queued jobs.

Resource budgets and queue administration belong to Job Server's dialogs/web UI;
Job Manager does not start another helper queue or apply SSH settings to it.

> [!WARNING]
> **Never expose Job Server to the public internet. Localhost, private LAN, or
> private Tailscale only, with trusted users.** Use HTTPS for LAN/Tailscale access;
> HTTPS is the default. Each host can explicitly enable **Allow trusted LAN HTTP**
> for literal RFC 1918 IPv4 or IPv6 ULA addresses and `.local` names. Public IPs, other
> DNS names and link-local addresses are rejected. Any LAN device can answer for a
> `.local` name, so over HTTP a password can reach the wrong device. Passwords, bearer tokens and job contents
> are unencrypted on HTTP; use only on a trusted LAN. The client bypasses HTTP
> environment proxies and refuses redirects. The server also needs its LAN HTTP
> option enabled and the chosen origin registered. Its listener remains loopback;
> use a restricted private reverse proxy on the same PC. Localhost works as before. No Funnel, public reverse proxies/tunnels,
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

## Login commands

**Advanced > Login commands** run on the server PC at the start of each job, before the
preset's pre-commands, as part of the job's script. Use them for setup such as
`conda activate` or `module load`, written for the server's shell: PowerShell on Windows,
Bash on Linux. They are not run for Host Monitor or file browsing, and **Read the login files first** does
not apply to Job Server hosts.

## Local HTTPS and CA registration

For Job Server's local HTTPS listener, copy its HTTPS LAN URL into the host's
**Server URL** field. On the server PC, open **Settings > Create / show CA
certificate** to see the SHA-256 fingerprint.

In the host form, choose **Trust local server CA...**. Job Manager downloads the
certificate without credentials and displays its fingerprint. Compare it with the
server PC and choose **Yes** only if they match; this comparison is what detects
interception during setup. The certificate must also be limited to private addresses
and localhost, or it is refused. Redirects and environment proxies are not used for
this bootstrap request. Normal API connections always verify the TLS certificate
and server name.

The confirmed public CA is saved under `identity/ca/` on all platforms.
On Windows, it is also registered in the current user's ROOT store, which
Python's standard TLS context reads. Other applications using that store also
trust the CA. On Mac/Linux, the CA is saved under Job Manager's data directory and
the connection's CA field is filled automatically. No system trust store is
modified on those platforms. Browser trust registration is managed separately.
Publicly trusted HTTPS, including Tailscale Serve, needs no local CA registration.

Choose **Sign in to Job Server...** and enter username and password in one dialog.
Subsequent connections use this application's saved device credential.

## Host Monitor

Use **Choose Hosts...** in Host Monitor to select the cards to display. The choice
is remembered, and newly added enabled hosts appear by default. Disabled hosts
are hidden automatically; enabling a host restores its previous visibility choice.
A Job Server host's card shows the server PC's CPU and memory use and the
server's queue: how many jobs run, how many wait, how much of the CPU budget
running jobs reserve, and whether an administrator paused the queue. The counts
include other users' jobs, since they decide how long new work waits. The card
reads the server's own report, so no shell command runs there; this device must
be signed in, otherwise the card says so. For SSH and local hosts, each host's
usage-monitoring setting controls sampling independently of card visibility.

While one of your jobs waits on Job Server 0.10 or newer, the job list's State
column reads, for example, `PENDING (3 ahead)` or `PENDING (next in line)`: the
number of jobs the server will start before it, in the server's own order
(forced runs, then priority, then submission time). Details shows the same as
**Queue position**. Job Server's web UI lists the whole queue, every user's job
by name and owner, without their commands, files or output.

The web monitor uses the same **Choose Hosts...** selection. Hiding a card
removes it and its job details from the published web snapshot immediately.

## File Browser (Job Server 0.7 or newer)

Select the server in **File Browser** to list the job folders your account may
access. The root contains job UUIDs; opening a folder reads only that folder.
The server's OS directories, control files and TLS material are not browsable.
Administrators may browse all jobs; other users may browse only their own.

Double-click a result to download and view it as text. **Download...** saves the
selected file or folder into a directory you choose. Folder downloads preserve
subfolders and empty directories, verify every file's SHA-256, skip existing
files, and reject linked destination paths. No server ZIP is extracted locally.

The common default is **Settings > Results > Download folder**. Turn off
**Save results beside the input file** to use that default for new jobs;
previously recorded job destinations are not moved. Temporary viewer copies
remain in the application cache. When running standalone, the file and text
viewers hide **Open in MoleditPy** because no host application can receive it.
