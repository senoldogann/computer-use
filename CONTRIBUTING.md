# Contributing

## What this project values

This is an agent that drives a real computer, so the bar is empirical: a change
is finished when it has been *measured* on a real machine, not when it compiles.
Most of the defects found so far were invisible to unit tests and only appeared
when the agent was pointed at a live desktop — a coordinate conversion applied
backwards, a verification witness that could not see text, an accessibility
walk that stopped one level above every web page.

So when you fix something, say what you measured. "10 steps cold, 5 warm" is
worth more in a commit message than a paragraph of intent.

## The four gates

Every commit must leave all four green. Run them before you push:

```bash
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m pyright --pythonpath .venv/bin/python
.venv/bin/python -m pytest tests/ -q
cargo test --manifest-path driver/Cargo.toml && \
  cargo clippy --manifest-path driver/Cargo.toml --all-targets -- -D warnings
```

A failing test is a finding, not an obstacle. Fix the cause; do not weaken the
assertion. If a test encoded behaviour a change deliberately alters, rewrite it
to assert the *new* contract and say so in the commit message.

## Setup

```bash
uv sync
cargo build --manifest-path driver/Cargo.toml --release
bash scripts/make_signing_cert.sh   # once: keeps the Accessibility grant across rebuilds
bash scripts/package_app.sh         # builds and installs ~/Applications/ComputerUse.app
```

macOS will ask for **Accessibility** and **Screen Recording** consent the first
time the driver runs. Both are required; the driver refuses to start without
the first and cannot see without the second.

An OpenAI key goes in `.env` or `~/.computeruse/env` — see `.env.example`.
Neither file is ever committed.

## Tests

Follow the existing strategy: integration and smoke tests that exercise real
behaviour, over unit tests that restate the implementation. The suite runs
against the driver's simulated backend, which models the *observable
consequences* of actions — an inert fixture would make every verified action
look like a miss.

Tests requiring the real host are the exception, not the default. If your
change can only be verified on hardware, say so in the pull request and
describe what you ran.

## Pull requests

Explain the root cause, not just the patch. What did the system do wrong, what
did you measure, and what does it do now? A reviewer should be able to tell
from the description whether the fix addresses the cause or a symptom.
