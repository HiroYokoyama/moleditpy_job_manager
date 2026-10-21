# Async lifetime audit (3.0.2)

The crash report ends in `QApplication.exec()` with a Windows access violation.
It does not include a native stack, so it cannot identify the faulting library
or prove which operation caused that crash. This audit addresses reproducible
QObject lifetime defects found while investigating it.

A Windows Application Error event matching the reported process identifies
`python313.dll` and exception `0xc0000005`. The referenced native dump is no
longer present. The module alone does not establish the originating Qt call.

## Pool senders

The plugin has two QRunnable implementations: `BackgroundTask` in `tasks.py`
and `_PollTask` in `poller.py`. Both previously created a parentless signal
QObject on the GUI thread. When the pool auto-deleted the runnable, its last
Python reference to that QObject could disappear on the pool thread, destroying
the GUI-affine sender there. Keeping the callback receiver alive alone did not
fix this sender lifetime.

Both now use `_SignalRunnable`: the application owns the sender independently
of the runnable, and runnable finalization schedules `deleteLater()` on the
sender. This covers completed work, failed work, and queued work discarded by
`QThreadPool.clear()`. It does not parent senders to a dialog that can disappear
while a worker is still emitting.

Tests use real pools and record the actual thread running `destroyed` with a
direct connection. All four success/failure cases for the two runnable types
fail when their original parentless construction is restored in memory, and
pass with the fix. Queued cancellation is tested separately.

## Callback receivers and UI consumers

All `run_async` call sites in the service, submission workflow, host sampler,
host settings, job monitor, webhook settings, web settings, and server enrollment
were inspected. The existing `_CallbackReceiver` delivers through QObject slots
and uses QObject ownership to drop queued results after an owner is destroyed.
Ownerless service/API operations use the pool/application receiver rather than
a temporary worker-owned closure receiver.

One owner mismatch was found: `JobsDialog._refresh_tail_file` targeted a separate
text window but used the monitor as owner. It now uses the actual text window.
Tests destroy that window before success and error results arrive and verify
neither callback runs.

The output-file download subscriptions explicitly disconnect on completion,
failure, refusal, and dialog exit. Host monitor, job monitor, presence, and status
widget subscriptions have their own QObject receivers or explicit disconnect
paths. UI-only button/action closures are not temporary pool senders.

## Other threads

The API server and web monitor keep their bridge/service alive independently of
individual HTTP requests. API requests use `_GuiBridge`; web requests emit
`WebService._requested` to dispatch sampling changes on the GUI thread. Their
socket serving threads do not own temporary GUI signal senders.

`webhook.post_async` uses a Python thread for an HTTP request and does not touch
Qt objects. Transport/Paramiko threads operate on connections, not widgets.
These paths do not instantiate either of the defective sender patterns.

## Limits

The original access violation was not reproduced. Native crash dumps and a
reproduction scenario would still be needed to attribute that specific crash.
The fixes and regression tests establish safe sender destruction and correct
ownership for the defects identified above.
