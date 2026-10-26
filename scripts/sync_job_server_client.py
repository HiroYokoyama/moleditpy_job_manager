"""Refresh the optional pure HTTP client from a pinned Job Server checkout."""

import argparse
import ast
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "85cf198b8082754a482b14c7ee1056375f930dc5"
FILES = ("client.py", "credentials.py", "files.py", "errors.py")


def sync(source):
    source = Path(source).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", SOURCE_COMMIT + "^{commit}"], text=True
    ).strip()
    if revision != SOURCE_COMMIT:
        raise ValueError("Source checkout does not match the audited client commit")

    def source_file(name):
        return subprocess.check_output(
            ["git", "-C", str(source), "show", SOURCE_COMMIT + ":" + name]
        )

    target = ROOT / "job_manager/job_server_client"
    target.mkdir(parents=True, exist_ok=True)
    files = {name: source_file("job_server/" + name).decode("utf-8") for name in FILES}
    auth_source = source_file("job_server/auth.py").decode("utf-8")
    signing = next(
        node
        for node in ast.parse(auth_source).body
        if isinstance(node, ast.FunctionDef) and node.name == "signing_message"
    )
    files["auth.py"] = (
        '"""Only protocol signing; no server, database or password authentication."""\n\nimport json\n\n\n'
        + ast.get_source_segment(auth_source, signing)
        + "\n"
    )
    files["__init__.py"] = (
        '"""Pinned optional Job Server client. Importing this package starts nothing."""\n'
    )
    for name, content in files.items():
        (target / name).write_text(content, "utf-8", newline="\n")
    manifest = {
        "repository": "https://github.com/HiroYokoyama/moleditpy_job_server",
        "commit": SOURCE_COMMIT,
        "upstream_license": "LGPL-3.0-or-later",
        "license": "LGPL-3.0-or-later",
        "files": {
            name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()
        },
    }
    (target / "PROVENANCE.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    for name in ("LICENSE", "COPYING", "LICENSING.md"):
        (target / name).write_bytes((ROOT / name).read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    sync(parser.parse_args().source)
