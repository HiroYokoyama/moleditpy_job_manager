"""Resolve and validate local API inputs independently of routing."""

from __future__ import annotations

import os
import time
from typing import Any, List, Mapping, Optional

from .api_types import PRESET_FIELDS, ApiError
from .input_names import check_input_name
from .models import HostProfile, Job, SubmitPreset


class ApiInputs:
    def __init__(self, store):
        self.store = store

    def _usable_host(self, wanted: str) -> HostProfile:
        host = self._host(wanted)
        if not host.enabled:
            raise ApiError(409, f"Host '{host.name}' is disabled in the Hosts dialog")
        return host

    @staticmethod
    def _remote_path(value: Any) -> str:
        path = str(value or "").strip()
        if not path:
            raise ApiError(400, "'path' is required: a path on the host")
        if any(ch in path for ch in ("\n", "\r", "\0")):
            raise ApiError(400, "A path on the host cannot contain a line break")
        return path

    @staticmethod
    def _query_flag(query: Mapping[str, str], key: str, default: bool) -> bool:
        value = str(query.get(key, "") or "").strip().lower()
        if not value:
            return default
        return value not in ("0", "false", "no", "off")

    def _host(self, wanted: str) -> HostProfile:
        """A host by id or by name; the name is what a script would name."""
        if not wanted:
            raise ApiError(400, "'host' is required: the id or name of a configured host")
        host = self.store.hosts.get(wanted)
        if host is not None:
            return host
        matches = [h for h in self.store.hosts.values() if h.name.lower() == wanted.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ApiError(409, f"More than one host is named '{wanted}'; use its id instead")
        known = ", ".join(sorted(h.name for h in self.store.hosts.values())) or "none configured"
        raise ApiError(404, f"No host called '{wanted}'. Known hosts: {known}")

    def _job(self, job_id: str) -> Job:
        job = self._job_or_none(job_id)
        if job is None:
            raise ApiError(404, f"No tracked job with id '{job_id}'")
        return job

    def _job_or_none(self, job_id: str) -> Optional[Job]:
        return self.store.jobs.get(str(job_id or ""))

    @staticmethod
    def _files(body: Mapping[str, Any]) -> List[str]:
        raw = body.get("files") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raise ApiError(400, "'files' must be a path or a list of paths")
        files: List[str] = []
        for entry in raw:
            # Absolute, because the server's working directory is MoleditPy's
            # and has nothing to do with the caller's.
            path = os.path.abspath(os.path.expanduser(str(entry)))
            if not os.path.isfile(path):
                raise ApiError(400, f"No such input file: {entry}")
            # Refused here as well as in the runner, so a caller is told what
            # is wrong with its request instead of watching a job fail.
            try:
                check_input_name(os.path.basename(path))
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            files.append(path)
        return files

    def _preset(self, host: HostProfile, body: Mapping[str, Any]) -> SubmitPreset:
        """The resource request, from a named preset and/or explicit fields.

        A submission with neither is refused rather than falling back to the
        dataclass default, whose command template names one particular program:
        a caller that forgot the command would otherwise have silently run
        ORCA on whatever it uploaded.
        """
        named = str(body.get("preset", "") or "").strip()
        command = body.get("command")
        if named:
            # A copy: the stored preset must not pick up this call's overrides.
            preset = SubmitPreset.from_dict(self._named_preset(host, named).to_dict())
            preset.id = SubmitPreset().id
        elif command:
            preset = SubmitPreset(host_id=host.id, name="api", command_template="")
        else:
            raise ApiError(
                400,
                "Give 'command' (the command line to run) or 'preset' (the name "
                "of a saved preset for this host).",
            )
        preset.host_id = host.id
        if command is not None:
            preset.command_template = str(command)
        for key, kind in PRESET_FIELDS.items():
            if key in body:
                setattr(preset, key, self._coerce(body[key], key, kind))
        if not preset.command_template.strip():
            raise ApiError(400, "The command line is empty")
        return preset

    def _named_preset(self, host: HostProfile, named: str) -> SubmitPreset:
        preset = self.store.presets.get(named)
        if preset is not None and preset.host_id == host.id:
            return preset
        for_host = self.store.presets_for_host(host.id)
        matches = [p for p in for_host if p.name.lower() == named.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ApiError(409, f"More than one preset on '{host.name}' is named '{named}'")
        known = ", ".join(p.name for p in for_host) or "none"
        raise ApiError(404, f"No preset '{named}' on host '{host.name}'. Known presets: {known}")

    @staticmethod
    def _coerce(value: Any, key: str, kind: type) -> Any:
        if kind is bool:
            if not isinstance(value, bool):
                raise ApiError(400, f"'{key}' must be true or false")
            return value
        if kind is int:
            # bool is an int in Python; a client sending true for 'nodes' has
            # made a mistake worth reporting rather than reading as 1.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ApiError(400, f"'{key}' must be a whole number")
            if value < 0:
                raise ApiError(400, f"'{key}' cannot be negative")
            return value
        if kind is list:
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise ApiError(400, f"'{key}' must be a list of strings")
            return [str(entry) for entry in value]
        return str(value)

    @staticmethod
    def _optional_bool(body: Mapping[str, Any], key: str, default: bool) -> bool:
        if key not in body:
            return bool(default)
        value = body[key]
        if not isinstance(value, bool):
            raise ApiError(400, f"'{key}' must be true or false")
        return value

    @staticmethod
    def _int(value: Any, key: str, minimum: Optional[int] = None) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, f"'{key}' must be a whole number") from exc
        if minimum is not None and number < minimum:
            raise ApiError(400, f"'{key}' must be at least {minimum}")
        return number

    @staticmethod
    def _start_after(body: Mapping[str, Any]) -> float:
        """``start_after`` as an epoch second, accepting either spelling.

        A number is one already; a string is a local time, which is what a
        person writes into a script and what the wizard's own field shows.
        """
        value = body.get("start_after", 0)
        if not value:
            return 0.0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value).strip().replace("Z", "")
        for shape in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                return time.mktime(time.strptime(text, shape))
            except ValueError:
                continue
        raise ApiError(
            400,
            "'start_after' must be an epoch second or a local time like 2026-01-31T18:30",
        )
