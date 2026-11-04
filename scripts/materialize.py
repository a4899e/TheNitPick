#!/usr/bin/env python3
"""Write a commit's files into an empty directory from raw git objects.

Library use: ``materialize(repo, dest, ref, base, pinned_regex, git=...)``
returns a list of warnings. CLI: ``materialize.py REPO DEST REF BASE PINNED_REGEX``
prints them.

Every blob is read with ``git cat-file --batch`` and written byte-for-byte.
That bypasses checkout entirely, so none of the following ever apply:
- hooks;
- attributes from any source (.gitattributes, core.attributesFile,
  info/attributes);
- smudge or process filters;
- eol conversion.

A reviewed branch's content is therefore never interpreted by git on the host,
only copied.

Agent-instruction paths (those matching PINNED_REGEX) come from BASE, not
REF:
- A pinned path that is a symlink on BASE is resolved as a path in the BASE
  tree, and written as a regular file with the target's content.
- When that isn't possible, the path is omitted with a warning: the target
  leaves the repo, is absent, or is itself a link.
- Pinned paths from REF are never written.

Pinned paths are matched case-insensitively, because macOS and Windows
filesystems are case-insensitive. The BASE copies are written first, so a
branch file differing only in case can't take their place.

Every tree path is validated component by component before anything is
written. ``.``, ``..`` and ``.git`` are refused everywhere; on Windows, so are
backslashes, colons (drive prefixes and streams), reserved device names and
trailing dots or spaces. As a result, a crafted tree entry can't escape
DEST. Files are created exclusively (O_EXCL), never through a symlink
(O_NOFOLLOW where the OS has it), and only under directories this function
made. A path that already exists (for example a case-only collision) is
skipped with a warning.

On Windows without symlink privileges (no Developer Mode), a symlink is
written as a small file holding the link text. That matches git's own
``core.symlinks=false`` behaviour.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys

SYMLINK, GITLINK = "120000", "160000"
REGULAR = ("100644", "100755")


#: git with fsmonitor off. Callers that also disable hooks pass their own prefix.
DEFAULT_GIT = ["git", "-c", "core.fsmonitor=false"]
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


def unsafe_path(path: str) -> bool:
    """True when a tree path could escape DEST or alias something else."""
    for part in path.split("/"):
        if part in ("", ".", "..") or part.lower() == ".git":
            return True
        # Backslashes and colons are ordinary filename characters on POSIX, but
        # path separators, drive prefixes or stream names on Windows.
        if os.name == "nt" and ("\\" in part or ":" in part or WINDOWS_RESERVED.match(part)
                                or part.endswith((".", " "))):
            return True
    return False


def tree_entries(git: list[str], repo: str, ref: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, object id) for every entry of REF's tree, recursively."""
    out = subprocess.run(
        git + ["-C", repo, "ls-tree", "-r", "-z", "--full-tree", ref],
        check=True, capture_output=True,
    ).stdout
    entries: dict[str, tuple[str, str]] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, _kind, oid = meta.decode().split(" ")
        entries[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return entries


class Blobs:
    """Reads blobs through one ``git cat-file --batch`` process (no filters)."""

    def __init__(self, git: list[str], repo: str) -> None:
        self.proc = subprocess.Popen(
            git + ["-C", repo, "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def read(self, oid: str) -> bytes:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(oid.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise RuntimeError(f"materialize: {oid} is not a blob")
        size = int(header[2])
        data = b""
        while len(data) < size:
            chunk = self.proc.stdout.read(size - len(data))
            if not chunk:
                raise RuntimeError(f"materialize: short read for {oid}")
            data += chunk
        self.proc.stdout.read(1)  # trailing newline
        return data

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def make_parents(dest: str, path: str) -> bool:
    """Create PATH's parent directories under DEST. False on a symlink or file in the way.

    O_NOFOLLOW guards only the last component. Without this check, a pinned base
    path under a directory the branch tracks as a symlink would be written
    outside DEST.
    """
    current = dest
    for part in path.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return False
        if os.path.lexists(current) and not os.path.isdir(current):
            return False  # the branch put a regular file where BASE has a directory
        if not os.path.lexists(current):
            os.mkdir(current)
    return True


def write_entry(dest: str, path: str, mode: str, data: bytes, warnings: list[str]) -> None:
    if unsafe_path(path):
        warnings.append(f"{path!r} is not a safe path on this platform; omitted it.")
        return
    full = os.path.join(dest, *path.split("/"))
    if os.path.lexists(full):
        warnings.append(f"{path} collides with an existing path (on a case-insensitive "
                        "filesystem, a name differing only in case); omitted it.")
        return
    if not make_parents(dest, path):
        warnings.append(f"{path} is under a path that is a symlink or a file in the "
                        "reviewed tree; omitted it.")
        return
    if mode == SYMLINK:
        try:
            os.symlink(os.fsdecode(data), full)
            return
        except OSError:
            if os.name != "nt":
                raise
            # Windows without symlink privilege: store the link text, as git
            # does with core.symlinks=false.
    perm = 0o755 if mode == "100755" else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(full, flags, perm)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def materialize(repo: str, dest: str, ref: str, base: str, pinned_regex: str,
                git: list[str] | None = None) -> list[str]:
    """Write REF's tree into DEST with pinned paths from BASE; return warnings."""
    git = git or DEFAULT_GIT
    pinned = re.compile(pinned_regex, re.IGNORECASE)
    head = tree_entries(git, repo, ref)
    base_tree = tree_entries(git, repo, base)
    warnings: list[str] = []
    blobs = Blobs(git, repo)
    try:
        # Pinned BASE files first, so they win any case-insensitive collision.
        write_pinned(dest, base, base_tree, pinned, blobs, warnings)
        for path, (mode, oid) in sorted(head.items()):
            if pinned.search(path):
                continue
            if mode == GITLINK:  # submodules become empty directories
                if unsafe_path(path) or not make_parents(dest, path + "/_"):
                    warnings.append(f"submodule {path!r} can't be placed safely; omitted it.")
                continue
            write_entry(dest, path, mode, blobs.read(oid), warnings)
    finally:
        blobs.close()
    return warnings


def write_pinned(dest: str, base: str, base_tree: dict[str, tuple[str, str]],
                 pinned: re.Pattern, blobs: Blobs, warnings: list[str]) -> None:
    """Write BASE's agent-instruction files, resolving symlinked ones in the BASE tree."""
    for path, (mode, oid) in sorted(base_tree.items()):
        if not pinned.search(path) or mode == GITLINK:
            continue
        if mode != SYMLINK:
            write_entry(dest, path, mode, blobs.read(oid), warnings)
            continue
        target = os.fsdecode(blobs.read(oid))
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        entry = base_tree.get(resolved)
        if (target.startswith("/") or resolved.startswith("..") or entry is None
                or entry[0] not in REGULAR):
            warnings.append(f"pinned {path} links to {target}, which can't be pinned "
                            f"from {base}; omitted it.")
            continue
        write_entry(dest, path, "100644", blobs.read(entry[1]), warnings)


if __name__ == "__main__":
    for warning in materialize(*sys.argv[1:6]):
        print(f"WARNING: {warning}")
