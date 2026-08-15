# Command placeholders

Every submit preset has a **command template**: the line actually run on the
host, e.g.

```
$(which orca) {input} > {stem}.out
```

`{...}` placeholders in it are substituted once, at submit time, by
`schedulers/base.py::format_command`. The same set is used everywhere a
command template appears: the submit wizard's command field, a saved
template, and a per-extension default.

| Placeholder  | Meaning                                                        |
|--------------|-----------------------------------------------------------------|
| `{input}`    | The uploaded file's name, e.g. `water.inp`                     |
| `{stem}`     | `{input}` without its extension, e.g. `water`                  |
| `{basename}` | Same as `{stem}`, kept for templates written before it existed |
| `{name}`     | The job's display name                                         |
| `{jobdir}`   | The job's directory on the host                                |
| `{nodes}`    | The preset's node count                                        |
| `{ntasks}`   | The preset's task count                                        |
| `{cpus}`     | The preset's CPUs-per-task                                     |
| `{memory}`   | The preset's memory request, as typed (e.g. `8GB`)              |
| `{queue}`    | The preset's queue/partition name                               |
| `{walltime}` | The preset's walltime, as typed (e.g. `24:00:00`)               |

A placeholder that has nothing to substitute (an empty queue, no memory
request) is replaced with an empty string, not left literally in the command.

## Where a command comes from

1. **Built-in templates** (`command_templates.py`) — one conventional
   invocation per program MoleditPy can write input for. Picking one from the
   submit wizard's dropdown fills the command field; it is never applied on
   top of a preset you already saved.
2. **A per-extension default** — "Use this command for every .inp" remembers
   the command you typed for every input with that extension from then on.
   `.inp` is written by ORCA, CP2K *and* GAMESS, so the wizard never guesses
   here; it remembers what *you* chose instead.
3. **A named template** — "Save current command as..." keeps a command (and
   its fetch patterns) under a name of your own, for anything not covered by
   the built-in list.

All three — including deleting a per-extension default or a named template,
which used to have no path in the UI at all — are managed from
**Manage templates...** at the bottom of the submit wizard's template
dropdown.
