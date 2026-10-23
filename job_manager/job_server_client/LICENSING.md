# Licensing and independently developed plugins

The first-party source code in **moleditpy_job_manager** is licensed under the GNU Lesser
General Public License, version 3 or (at your option) any later version
(LGPL-3.0-or-later). `LICENSE` contains LGPLv3's additional permissions;
`COPYING` contains the incorporated GPLv3 terms. Earlier releases retain their
original licenses.

Copyright: Hiromichi Yokoyama / HiroYokoyama and the contributors identified in
the existing copyright notices. Retain those notices, `LICENSE`, `COPYING`, and
this document when redistributing the covered source or binaries.

Modifications to the covered code remain subject to the LGPL. Distribution
terms must allow users to modify the covered components and reverse engineer
for debugging their modifications as required by LGPLv3. Provide corresponding
source and allow modification or replacement of covered libraries when
distributing a combined binary, as required by the license.

Independently developed plugins that use MoleditPy's documented plugin API may
be distributed under any license, including proprietary licenses, under the
[main application's copyright-holder permission](https://github.com/HiroYokoyama/python_molecular_editor/blob/main/LICENSING.md).
Their authors choose their licenses and retain their copyright. This repository's
LGPL license does not change the licenses of other independently developed
plugins, copied MoleditPy code, or third-party components.

Third-party libraries, executables, models, and data retain their own licenses
and obligations. Preserve their notices and comply with the terms of the
versions actually redistributed, including native and transitive dependencies.
Running a program in a separate process does not by itself establish license
compatibility.

## Pinned first-party HTTP client

The owner also grants LGPL-3.0-or-later for the first-party client copied from
Job Server commit `b06bf900c703322ab526867964f8ff917936c01e`, including the
extracted `signing_message` function. The source commit and file hashes in
`job_manager/job_server_client/PROVENANCE.json` remain provenance records.
Its original upstream GPL-3.0-only grant is recorded separately; the additional
LGPL permission applies to the client copies distributed here. No server-side
or third-party code is implicitly relicensed by this client-specific grant.
