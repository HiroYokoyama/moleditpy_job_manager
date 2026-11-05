"""Reading a file and writing it back changes nothing that was not edited."""

from __future__ import annotations

import codecs

import pytest

from job_manager.text_document import TextFormat, copy_file, read_document, write_bytes


@pytest.mark.parametrize(
    "data",
    [
        b"one\ntwo\n",
        b"one\r\ntwo\r\n",
        b"one\rtwo\r",
        b"no newline at the end",
        b"",
        "水素\r\n".encode("utf-8"),
        codecs.BOM_UTF8 + "水素\n".encode("utf-8"),
        "水素\r\n".encode("cp932"),
        codecs.BOM_UTF16_LE + "水素\r\n".encode("utf-16-le"),
        codecs.BOM_UTF16_BE + "水素\n".encode("utf-16-be"),
        codecs.BOM_UTF32_LE + "水素\n".encode("utf-32-le"),
        b"caf\xe9 \xff\n",
        "a b\n".encode("utf-8"),
    ],
)
def test_an_unedited_round_trip_is_byte_for_byte(tmp_path, data):
    path = tmp_path / "a.inp"
    path.write_bytes(data)

    document = read_document(str(path))

    assert document.editable
    assert "\r" not in document.text
    assert document.format.encode(document.text) == data


def test_an_edit_keeps_encoding_and_line_endings(tmp_path):
    path = tmp_path / "a.inp"
    path.write_bytes("水素\r\n".encode("cp932"))

    document = read_document(str(path))
    edited = document.format.encode(document.text + "酸素\n")

    assert edited == "水素\r\n酸素\r\n".encode("cp932")
    assert document.format.newline == "\r\n"


def test_mixed_line_endings_are_reported_and_the_commonest_is_used(tmp_path):
    path = tmp_path / "a.inp"
    path.write_bytes(b"a\r\nb\r\nc\n")

    document = read_document(str(path))

    assert document.format.mixed_newlines
    assert document.format.newline == "\r\n"
    assert "Mixed" in document.note
    assert "mixed" in document.format.label()


def test_a_file_over_the_limit_is_read_only_and_says_so(tmp_path):
    path = tmp_path / "big.out"
    path.write_bytes(b"line\n" * 100)

    document = read_document(str(path), limit=50)

    assert not document.editable
    assert "Showing the last" in document.text
    assert "only its end" in document.note


def test_a_broken_utf16_file_cannot_be_edited(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(codecs.BOM_UTF16_LE + b"a\x00\x00\xd8")

    assert not read_document(str(path)).editable


def test_reading_as_a_chosen_encoding(tmp_path):
    path = tmp_path / "a.inp"
    path.write_bytes("水".encode("euc_jp"))

    document = read_document(str(path), encoding=("euc_jp", b""))

    assert document.text == "水"
    assert document.format.encoding_label() == "EUC-JP"


def test_reading_as_an_encoding_the_file_is_not_in_raises(tmp_path):
    path = tmp_path / "a.inp"
    path.write_bytes(b"\xff\xff")

    with pytest.raises(UnicodeError):
        read_document(str(path), encoding=("utf-8", b""))


def test_a_character_the_encoding_lacks_is_an_error():
    with pytest.raises(UnicodeEncodeError):
        TextFormat("cp932").encode("\U0001f600")


def test_a_failed_write_leaves_the_file_and_no_temporary(tmp_path):
    target = tmp_path / "a.inp"
    target.write_bytes(b"before")

    with pytest.raises(OSError):
        copy_file(str(tmp_path / "missing"), str(target))

    assert target.read_bytes() == b"before"
    assert [item.name for item in tmp_path.iterdir()] == ["a.inp"]


def test_copying_a_file_over_itself_leaves_it_alone(tmp_path):
    target = tmp_path / "a.inp"
    target.write_bytes(b"same")

    copy_file(str(target), str(target))

    assert target.read_bytes() == b"same"


def test_writing_replaces_the_content(tmp_path):
    target = tmp_path / "a.inp"
    target.write_bytes(b"before")

    write_bytes(str(target), b"after")

    assert target.read_bytes() == b"after"
    assert [item.name for item in tmp_path.iterdir()] == ["a.inp"]
