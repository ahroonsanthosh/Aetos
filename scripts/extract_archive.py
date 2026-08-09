"""Unpack selected members of a .zip or .tar.gz.

Used by `install-basemap.sh` instead of `unzip` and `tar`, because **Git Bash ships neither
reliably** — and Git Bash is the shell every Windows user of this repository is in, since
`start.sh` requires it. Python is already a hard requirement for running Aetos at all, so
leaning on its standard library here costs nothing and closes two platform gaps that would
otherwise fail only on Windows, which is the platform least likely to be the one a change was
tested on.

    python scripts/extract_archive.py ARCHIVE DEST PATTERN

`PATTERN` is a plain substring matched against each member's path. A pattern containing `/`
preserves the archive's directory structure under `DEST`; one without it writes matching files
flat into `DEST`, which is what pulling a single binary out of a release archive wants.

Exits non-zero with a message if nothing matches, so a caller can treat "downloaded something
that did not contain what we asked for" as the failure it is rather than silently continuing
with an empty directory.
"""

from __future__ import annotations

import pathlib
import sys
import tarfile
import zipfile


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2

    archive = pathlib.Path(argv[1])
    dest = pathlib.Path(argv[2])
    pattern = argv[3]
    keep_structure = "/" in pattern

    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def target_for(name: str) -> pathlib.Path | None:
        """Where a member should land, or None if it would escape the destination.

        The escape check is not paranoia about these two specific downloads: it is that an
        extractor which trusts archive paths is a footgun left lying around for whoever reuses
        it next, and the check costs one comparison.
        """
        candidate = (dest / name) if keep_structure else (dest / pathlib.Path(name).name)
        return candidate if candidate.resolve().is_relative_to(root) else None

    written = 0

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if pattern not in member or member.endswith("/"):
                    continue
                target = target_for(member)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(member))
                written += 1
    else:
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if pattern not in member.name or not member.isfile():
                    continue
                target = target_for(member.name)
                if target is None:
                    continue
                source = bundle.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
                written += 1

    if written == 0:
        print(f"{archive}: nothing matching {pattern!r}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
