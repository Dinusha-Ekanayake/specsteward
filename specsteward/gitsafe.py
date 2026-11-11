"""Git, run so that the repository's own settings cannot run a program or hide a change.

specsteward asks git what changed. That answer decides whether a gate, a review and the scope
checks still describe the code, so it must not be something the agent can bend:

* `core.fsmonitor` names a program git runs whenever it reads the index. A value written into
  `.git/config` would run inside specsteward's own process, outside the agent's sandbox, so
  every git call here switches it off.
* `git update-index --assume-unchanged` / `--skip-worktree` make git report an edited file as
  unchanged. `hidden_edits` finds those files by content.
* `.git/info/exclude` and `core.excludesFile` hide new files. `exclude_state` is recorded when
  a run starts, and a change to it during the run is reported.
"""

import hashlib
import subprocess
from pathlib import Path

GIT = ("git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false")
MAX_FLAGGED = 500  # more flagged files than this are all treated as hidden edits


def git_env() -> dict[str, str]:
    """The environment for git: no phone bot token, no prompts."""
    from specsteward.remote import env_without_tokens  # late: remote imports trust

    return {**env_without_tokens(), "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


def output(root: Path, *args: str) -> bytes | None:
    """stdout of `git <args>` in ``root``, or None when git fails or is missing."""
    try:
        proc = subprocess.run(
            [*GIT, *args], cwd=root, capture_output=True, check=False, env=git_env()
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _names(raw: bytes | None) -> list[str]:
    return [name for name in (raw or b"").decode("utf-8", "replace").split("\0") if name]


def hidden_edits(root: Path) -> list[str]:
    """Tracked files git was told to ignore (assume-unchanged, skip-worktree) whose content
    on disk is not what the index holds. Sparse checkouts flag files too, but those are not
    on disk, so they are not listed."""
    flagged = [
        entry[2:].replace("\\", "/")
        for entry in _names(output(root, "ls-files", "-v", "-z"))
        if len(entry) > 2 and (entry[0].islower() or entry[0] == "S")
    ]
    flagged = [path for path in flagged if (root / path).is_file()]
    if not flagged or len(flagged) > MAX_FLAGGED:
        return sorted(flagged)
    staged: dict[str, str] = {}
    for entry in _names(output(root, "ls-files", "-s", "-z", "--", *flagged)):
        meta, _, path = entry.partition("\t")
        staged[path.replace("\\", "/")] = meta.split(" ")[1] if " " in meta else ""
    actual = (output(root, "hash-object", "--", *flagged) or b"").decode().split()
    if len(actual) != len(flagged):
        return sorted(flagged)  # cannot tell: treat every flagged file as edited
    return sorted(
        path for path, sha in zip(flagged, actual, strict=True) if staged.get(path) != sha
    )


def exclude_state(root: Path) -> str | None:
    """Digest of the settings that hide untracked files from git, outside `.gitignore`:
    `.git/info/exclude` and the file `core.excludesFile` names. None without a repository."""
    where = output(root, "rev-parse", "--git-path", "info/exclude")
    if where is None:
        return None
    digest = hashlib.sha256()
    named = (output(root, "config", "--get", "core.excludesFile") or b"").decode().strip()
    digest.update(named.encode() + b"\0")
    for file in (root / where.decode().strip(), Path(named).expanduser() if named else None):
        try:
            digest.update(file.read_bytes() if file is not None and file.is_file() else b"")
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()
