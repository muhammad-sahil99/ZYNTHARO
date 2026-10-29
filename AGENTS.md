# AGENTS.md — rules for any agent working in this repository

## The one rule that matters

**Nothing ships that looks like it works.** If a capability cannot be executed on this machine
(a driver is missing, there is no licence, there is no hardware, there is no account), the code
must fail with a precise reason, the interface must show that reason, and the Capability Matrix
must mark the tool as unavailable or provisional. A fabricated success, a plausible default reading,
a silently skipped type, or a test that asserts the mock back is worse than a missing feature: it
corrupts every later decision made from it.

`python tools/reality_gate.py` exists to enforce this and must stay clean.

## Commands

```bash
python -m unittest discover -s tests -q      # the whole suite (fast; no network, no desktop)
python -m unittest tests.test_sensors -q     # one module
python tools/reality_gate.py                 # no fakes/markers/test doubles in shipped code
python tools/capability_matrix.py            # regenerate CAPABILITY_MATRIX.md + artifacts/capabilities.json
python tools/hud_coverage.py                 # regenerate hud/coverage.json
node tools/hud_visual_check.js               # HUD visual layer, no browser needed
python tools/release_gate.py --milestone M0  # the master gate
zyntharo doctor                                # which drivers exist on this host
```

Dependencies are optional extras in `pyproject.toml`. Tests must pass with **none** of them
installed: that is why every driver is imported lazily and every failure path is exercised.

## Architecture rules

* `zyntharo/cli.py::build_stack` is the single assembly point. A new subsystem must be created
  there, handed to everything that needs it, and stopped in `Stack.shutdown` — a component that is
  built but not wired is the failure this project already had once.
* Actions are declared with the `@action` decorator and are gated by capability. New capabilities
  go in `zyntharo/config.py::CAPABILITIES`; irreversible ones in `zyntharo/ops/risk.py` (the single
  copy — do not duplicate that list).
* Every event topic is a constant in `zyntharo/events.py` and must have a handler in `hud/app.js`;
  `tools/hud_coverage.py` fails otherwise.
* Rules that decide safety (verification tiers, irreversible capabilities, sensor thresholds)
  live in one module and are read by everyone. Two copies drift toward less protection.
* Long work must be interruptible: no unbounded loops, bounded batches, `stop_event`/`_stop`
  support, and a way to see what it is doing.

## Style

* Python 3.11+, `from __future__ import annotations`, dataclasses with `slots=True` where it fits.
* Docstrings explain *why* a decision was made, especially where a naive version is wrong.
* Tests are named for the behaviour that would break, and assert the honest failure as well as the
  happy path (a missing driver, a suspect reading, a refused action).
* No new dependency without a reason in the commit message and a lazy import so the suite still
  runs without it.

## Documentation that must stay true

`BUILD_STATUS.md` (what runs), `LIMITATIONS.md` (what does not, and why), `OWNER_REQUIREMENTS.md`
(what only the owner can provide), `TRACEABILITY.md` (spec section → code → test). A change that
alters any of those four without updating them is incomplete.