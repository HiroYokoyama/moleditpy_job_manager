import base64
import ctypes
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .files import atomic_write, open_regular


def dpapi(data, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(output.data)


def save_key(path, device_id, server_id, key):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_key = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    data = json.dumps(
        {"device_id": device_id, "server_id": server_id, "key": base64.b64encode(raw_key).decode()}
    ).encode()
    if os.name == "nt":
        data = b"DPAPI\n" + dpapi(data)
    atomic_write(path, data)


def load_key(path):
    path = Path(path)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Private key file must be accessible only to its owner (chmod 600)")
    with open_regular(path) as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Credential file is too large")
    if data.startswith(b"DPAPI\n"):
        if os.name != "nt":
            raise ValueError("This credential is encrypted for its Windows account")
        data = dpapi(data[6:], decrypt=True)
    elif os.name == "nt":
        raise ValueError("Windows credentials must use DPAPI")
    result = json.loads(data)
    result["private_key"] = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(result.pop("key"))
    )
    return result
