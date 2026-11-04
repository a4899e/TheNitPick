#!/usr/bin/env python3
"""Write a commit's files into an empty directory from raw git objects.

Usage: materialize.py GIT_DIR_REPO DEST REF BASE PINNED_REGEX

Every blob is read with ``git cat-file --batch`` and written byte-for-byte.
That bypasses checkout entirely: no hooks, no attributes from any source
(.gitattributes, core.attributesFile, info/attributes), no smudge or process
filters, no eol conversion. A reviewed branch's content is therefore never
interpreted by git on the host, only copied.

Agent-instruction paths (those matching PINNED_REGEX) are taken from BASE
instead of REF. A pinned path that is a symlink on BASE is resolved as a path
in the BASE tree and written as a regular file with the target's content. When
that isn't possible (the target leaves the repo, is absent, or is itself a
link), the path is omitted and a warning printed. Pinned paths from REF are
never written.

Files are created with O_EXCL and O_NOFOLLOW under directories this script
made, so nothing is written through a symlink. Prints one ``WARNING:`` line
per pinned path it had to drop.
"""

import os
import posixpath
import re
import subprocess
import sys

SYMLINK, GITLINK = "120000", "160000"


def tree_entries(repo: str, ref: str) -> "dict[str, tuple[str, str]]":
    """path -> (mode, object id) for every entry of REF's tree, recursively."""
    out = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-r", "-z", "--full-tree", ref],
        check=True, capture_output=True,
    ).stdout
    entries = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, _kind, oid = meta.decode().split(" ")
        entries[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return entries


class Blobs:
    """Reads blobs through one ``git cat-file --batch`` process (no filters)."""

    def __init__(self, repo: str) -> None:
        self.proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )

    def read(self, oid: str) -> bytes:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(oid.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise SystemExit(f"materialize: {oid} is not a blob")
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)  # trailing newline
        return data

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def make_parents(dest: str, path: str) -> bool:
    """Create PATH's parent directories under DEST, refusing to pass through a symlink.

    O_NOFOLLOW guards only the last component. Without this check, a pinned base
    path under a directory the branch tracks as a symlink would be written
    outside DEST.
    """
    current = dest
    for part in path.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            return False
        if not os.path.isdir(current):
            os.mkdir(current)
    return True


def write_entry(dest: str, path: str, mode: str, data: bytes) -> None:
    full = os.path.join(dest, path)
    if not make_parents(dest, path):
        print(f"WARNING: {path} is under a symlinked directory; omitted it.")
        return
    if mode == SYMLINK:
        os.symlink(os.fsdecode(data), full)
        return
    perm = 0o755 if mode == "100755" else 0o644
    fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, perm)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def main() -> None:
    repo, dest, ref, base, pinned_regex = sys.argv[1:6]
    pinned = re.compile(pinned_regex)
    head = tree_entries(repo, ref)
    base_tree = tree_entries(repo, base)
    blobs = Blobs(repo)
    try:
        for path, (mode, oid) in sorted(head.items()):
            if pinned.search(path):
                continue
            if mode == GITLINK:
                os.makedirs(os.path.join(dest, path), exist_ok=True)
                continue
            write_entry(dest, path, mode, blobs.read(oid))
        for path, (mode, oid) in sorted(base_tree.items()):
            if not pinned.search(path) or mode == GITLINK:
                continue
            if mode != SYMLINK:
                write_entry(dest, path, mode, blobs.read(oid))
                continue
            target = os.fsdecode(blobs.read(oid))
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            entry = base_tree.get(resolved)
            if (target.startswith("/") or resolved.startswith("..") or entry is None
                    or entry[0] not in ("100644", "100755")):
                print(f"WARNING: pinned {path} links to {target}, which can't be pinned "
                      f"from {base}; omitted it.")
                continue
            write_entry(dest, path, "100644", blobs.read(entry[1]))
    finally:
        blobs.close()


if __name__ == "__main__":
    main()
