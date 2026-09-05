# Audit probes

Empirical probes that answer questions the unit suite cannot: what the system
does with a real driver, a live socket, a wedged process, and an approval that
a human has to answer.

**These do not run under `pytest`.** The filenames are deliberately not
`test_*.py`, so collection skips them and CI never starts a driver. They are
run by hand, and their output is evidence, not a pass/fail verdict — each probe
prints a JSON line describing what it observed. Reading them is the point.

```
.venv/bin/python tests/audit/run_audit.py
```

That runs the five automated suites:

| Probe | What it observes |
|---|---|
| `logic_probe.py` | permission classification, coordinate bounds, kill-switch channels, credential guard |
| `web_probe.py` | SSRF guard against loopback and its aliases |
| `extra_probe.py` | actuation ordering vs the bounds gate, auditor behaviour on transport failure |
| `cli_probe.py` | approval lifecycle across runs, mission identity after a failed finish |
| `driver_probe.py` | driver IPC, a frozen driver, restart after SIGKILL, real-backend health |

Three more are run individually because they need a real screen, a real hotkey
tap, or a release build: `native_probe.py`, `release_kill_probe.py`, and the
`AuditProbe.swift` / `trajectory_probe.rs` helpers.

Session logs land under `target/`, which is git-ignored.

## Reading the output

A probe "passing" means it ran, not that the system behaved well. The 2026-09-05
run, for example, completed 5/5 while recording a loopback SSRF bypass
(`127.1` resolving past the guard that blocks `127.0.0.1`) and a kill switch
whose shake monitor was never polled. Both became fixes. Treat every JSON line
as a measurement to interpret.
