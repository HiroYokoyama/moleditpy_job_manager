"""Read and write a text file so that an edit changes only what was edited.

Pure Python, no Qt. A file's encoding, byte order mark and line endings belong to
the file: a Shift-JIS input written back as UTF-8, or a CRLF one written back as
LF, is a different file to the program that reads it.
"""

from __future__ import annotations

import codecs
import locale
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

#: The most of a text file shown at once; the end is kept, since that is where
#: an output file says how it finished.
TEXT_VIEW_LIMIT = 5 * 1024 * 1024

#: What the viewer offers: (label, codec, byte order mark).
ENCODINGS: Tuple[Tuple[str, str, bytes], ...] = (
    ("UTF-8", "utf-8", b""),
    ("UTF-8 with BOM", "utf-8", codecs.BOM_UTF8),
    ("Shift_JIS (CP932)", "cp932", b""),
    ("EUC-JP", "euc_jp", b""),
    ("UTF-16 LE", "utf-16-le", codecs.BOM_UTF16_LE),
    ("UTF-16 BE", "utf-16-be", codecs.BOM_UTF16_BE),
    ("Latin-1", "latin-1", b""),
)
NEWLINES: Tuple[Tuple[str, str], ...] = (("LF", "\n"), ("CRLF", "\r\n"), ("CR", "\r"))

_BOMS = (
    # UTF-32 first: its little-endian mark begins with UTF-16's.
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


@dataclass(frozen=True)
class TextFormat:
    """Everything about a file's bytes that its text does not say."""

    encoding: str = "utf-8"
    bom: bytes = b""
    #: The file's line ending; the most common one if it mixes them.
    newline: str = "\n"
    mixed_newlines: bool = False

    def encode(self, text: str) -> bytes:
        """``text``, with lines ended by ``\\n``, as this format's bytes."""
        return self.bom + text.replace("\n", self.newline).encode(self.encoding)

    def encoding_label(self) -> str:
        for label, encoding, bom in ENCODINGS:
            if (encoding, bom) == (self.encoding, self.bom):
                return label
        return self.encoding.upper() + (" with BOM" if self.bom else "")

    def label(self) -> str:
        newline = dict((value, name) for name, value in NEWLINES)[self.newline]
        if self.mixed_newlines:
            newline = f"mixed, mostly {newline}"
        return f"{self.encoding_label()} | {newline}"


@dataclass(frozen=True)
class TextDocument:
    text: str
    format: TextFormat
    #: False when the text is not the whole file, or not exactly the file.
    editable: bool = True
    note: str = ""


def _text_encoding(sample: bytes) -> str:
    """The encoding a byte order mark names, or UTF-8 without one."""
    for marker, encoding in _BOMS:
        if sample.startswith(marker):
            return encoding
    return "utf-8"


def read_text_for_view(path: str, limit: int = TEXT_VIEW_LIMIT) -> str:
    """The file as text, or its last ``limit`` bytes with a line saying so."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        encoding = _text_encoding(handle.read(4))
        offset = 0
        if size > limit:
            offset = size - limit
            unit = 4 if encoding.startswith("utf-32") else 2 if encoding.startswith("utf-16") else 1
            offset += -offset % unit
        handle.seek(offset)
        # The file may grow between stat and read (a running calculation).
        data = handle.read(limit)
    text = data.decode(encoding, errors="replace").removeprefix("﻿").replace("\r\n", "\n")
    if size > limit:
        # Cut at a line boundary, so the first line shown is a whole one.
        text = text.split("\n", 1)[-1]
        text = f"[Showing the last {limit // (1024 * 1024)} MB of {size:,} bytes]\n\n" + text
    return text


def _candidates() -> List[str]:
    """Encodings to try on a file with no byte order mark, most likely first.

    Latin-1 is last and cannot fail, so a file in anything unforeseen still
    round-trips byte for byte even if it reads as nonsense.
    """
    unique: List[str] = []
    seen = set()
    for name in ("utf-8", locale.getpreferredencoding(False), "cp932", "latin-1"):
        try:
            key = codecs.lookup(name).name
        except LookupError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def _round_trip(body: bytes, encoding: str) -> str:
    """``body`` decoded; raises UnicodeError if it would not encode back the same."""
    raw = body.decode(encoding)
    if raw.encode(encoding) != body:
        raise UnicodeError(f"{encoding} does not reproduce these bytes")
    return raw


def _detect(data: bytes) -> Tuple[str, bytes, str, bool]:
    """(encoding, byte order mark, raw text, lossless) for a whole file."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            body = data[len(bom) :]
            try:
                return encoding, bom, _round_trip(body, encoding), True
            except UnicodeError:
                return encoding, bom, body.decode(encoding, errors="replace"), False
    for encoding in _candidates():
        try:
            return encoding, b"", _round_trip(data, encoding), True
        except UnicodeError:
            continue
    return "utf-8", b"", data.decode("utf-8", errors="replace"), False


def _document(raw: str, encoding: str, bom: bytes, lossless: bool) -> TextDocument:
    crlf = raw.count("\r\n")
    counts = {"\n": raw.count("\n") - crlf, "\r\n": crlf, "\r": raw.count("\r") - crlf}
    newline = max(counts, key=lambda ending: counts[ending])
    mixed = sum(1 for count in counts.values() if count) > 1
    text_format = TextFormat(encoding, bom, newline, mixed)
    note = ""
    if not lossless:
        note = "This file has bytes its encoding cannot represent, so it cannot be edited here."
    elif mixed:
        note = "Mixed line endings. Saving an edit uses one line ending throughout."
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return TextDocument(text, text_format, editable=lossless, note=note)


def read_document(
    path: str,
    limit: int = TEXT_VIEW_LIMIT,
    encoding: Optional[Tuple[str, bytes]] = None,
) -> TextDocument:
    """The file at ``path``; only its end, read only, if larger than ``limit``.

    ``encoding`` is (codec, byte order mark) to read it as instead of detecting,
    and raises UnicodeError when the file is not in that encoding.
    """
    size = os.path.getsize(path)
    data = b""
    if size <= limit:
        with open(path, "rb") as handle:
            # One more than the limit: the file may grow after the stat.
            data = handle.read(limit + 1)
    if size > limit or len(data) > limit:
        with open(path, "rb") as handle:
            sample = handle.read(4)
        bom = next((mark for mark, _name in _BOMS if sample.startswith(mark)), b"")
        return TextDocument(
            read_text_for_view(path, limit),
            TextFormat(_text_encoding(sample), bom),
            editable=False,
            note="Too large to edit here, so only its end is shown. Save As copies the whole file.",
        )
    if encoding is not None:
        codec, bom = encoding
        body = data[len(bom) :] if bom and data.startswith(bom) else data
        return _document(_round_trip(body, codec), codec, bom, True)
    return _document(*_reordered(_detect(data)))


def _reordered(detected: Tuple[str, bytes, str, bool]) -> Tuple[str, str, bytes, bool]:
    encoding, bom, raw, lossless = detected
    return raw, encoding, bom, lossless


def file_stamp(path: str) -> Optional[Tuple[int, int]]:
    """What changes when something else writes the file, or None if it is gone."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def _replace_file(target: str, write: Callable[[str], object]) -> None:
    """Write beside ``target`` and move into place, so a failure leaves it intact."""
    target = os.path.abspath(target)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.", dir=os.path.dirname(target)
    )
    os.close(handle)
    try:
        write(temporary)
        if os.path.exists(target):
            shutil.copymode(target, temporary)
        else:
            # mkstemp is owner-only, which is not what a saved file should be.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(temporary, 0o666 & ~umask)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def write_bytes(target: str, data: bytes) -> None:
    def write(path: str) -> None:
        with open(path, "wb") as handle:
            handle.write(data)

    _replace_file(target, write)


def copy_file(source: str, target: str) -> None:
    """Copy byte for byte; copying a file over itself leaves it alone."""
    if os.path.exists(target) and os.path.samefile(source, target):
        return
    _replace_file(target, lambda path: shutil.copyfile(source, path))


__all__ = [
    "ENCODINGS",
    "NEWLINES",
    "TEXT_VIEW_LIMIT",
    "TextDocument",
    "TextFormat",
    "copy_file",
    "file_stamp",
    "read_document",
    "read_text_for_view",
    "write_bytes",
]
