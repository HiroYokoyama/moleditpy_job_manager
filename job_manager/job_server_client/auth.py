"""Only protocol signing; no server, database or password authentication."""

import json


def signing_message(server_id, device_id, nonce):
    return json.dumps(
        {
            "purpose": "moleditpy-job-server-access",
            "protocol_version": "1",
            "server_id": server_id,
            "device_id": device_id,
            "nonce": nonce,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
