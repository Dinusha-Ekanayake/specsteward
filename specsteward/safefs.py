"""Refuse to write through symlinks that a repository ships.

A cloned repository can commit ``.sdd/`` as a symlink, or a dangling ``.graphifyignore``
symlink, so that specsteward's own writes (state, logs, the shim, ignore files) land in a
file outside the project, such as a shell profile. Every write goes through this check.
"""

import os
from pathlib import Path


def ensure_safe(root: Path, path: Path) -> None:
    """Raise ValueError if ``path`` is outside ``root`` or any part of it below ``root``
    (folders or the file itself) is a symlink."""
    path = Path(os.path.normpath(path))  # `root/../x` must not pass for being under root
    root = Path(os.path.normpath(root))
    try:
        rel = path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"refusing to write outside the project: {path}") from error
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"refusing to write through a symlink: {current.relative_to(root)}")
