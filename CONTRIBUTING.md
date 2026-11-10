# Contributing

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt         # Windows: .venv/Scripts/pip
bash scripts/sdd.sh init                                       # the repo uses itself
bash .sdd/bin/sdd trust                                 # once, after reviewing sdd.toml
bash .sdd/bin/sdd check                                 # lint, format, types, tests (85% coverage floor)
```

- Standard library only for runtime code. No runtime dependencies.
- Python 3.11+, strict mypy, ruff.
- Add a test for every change; the launcher, hooks and CLI have end-to-end tests that run through `bash`.
- Keep shell scripts LF (`.gitattributes` enforces it) and presets ASCII-only.
- Commit messages: Conventional Commits (`feat|fix|perf|docs|test|ci|chore: summary`).
- Pinned tools: `BUN_VERSION` and the install scripts' `SCRIPT_SHA256`
  (`specsteward/prereqs.py`), and `GRAPHIFY_VERSION` (`specsteward/config.py`) with
  `specsteward/graphify-constraints.txt`. Bump them deliberately, after reading the release
  notes, in a commit of their own: for Bun, take the sha256 of `src/runtime/cli/install.sh`
  and `install.ps1` at the new tag; for graphify, install the new version with uv and write
  `uv pip freeze` of its tool environment into the constraints file. CI installs the lock on
  every OS.
- Releases: bump `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and `pyproject.toml` together and date the
  `CHANGELOG.md` section; a test checks that they agree. Also update the pinned-version
  example in the README's install section.
- Changing an agent or skill prompt? Say what failure it fixes and, if possible, add a unit test for the script side.
