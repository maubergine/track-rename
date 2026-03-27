#!/usr/bin/env python3
"""
MP4 Chunk-Offset Corruption Fixer
-----------------------------------
Scans M4A/MP4 files for stco/co64 chunk-offset corruption introduced by an
earlier version of tag_classical.py.

That version inserted the shwm atom inside the moov box (which precedes mdat
in normally-structured files) but did not update the stco/co64 chunk-offset
tables that store absolute byte positions pointing into mdat.  Growing moov
by 25 bytes pushed mdat 25 bytes later in the file, leaving every stored
offset 25 bytes short.  The audio decoder then reads from the wrong positions
and the file is unplayable even though its atom structure looks intact.

Detection: a file is considered corrupted when it contains a shwm atom AND
at least one stco or co64 entry points to a byte position before the start
of the mdat atom.

Repair: add 25 (the size of the shwm atom that was inserted) to every stco
and co64 entry in the file.

Usage:
    python3 fix_chunk_offsets.py --scan     [Library.xml]
    python3 fix_chunk_offsets.py --fix      [Library.xml]
    python3 fix_chunk_offsets.py -h

Requires: Python 3.10+, standard library only.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit(
        "Error: fix_chunk_offsets.py requires Python 3.10 or later.\n"
        "You are running Python {}.{}.".format(*sys.version_info[:2])
    )

import os
import plistlib
import tempfile
from collections import defaultdict
from urllib.parse import unquote


# ---------------------------------------------------------------------------
# Terminal colours (disabled when not a tty)
# ---------------------------------------------------------------------------

def _make_colours(enabled: bool):
    if enabled:
        return dict(
            GREEN='\033[32m', YELLOW='\033[33m', CYAN='\033[36m',
            RED='\033[31m', BOLD='\033[1m', RESET='\033[0m', DIM='\033[2m',
        )
    return defaultdict(str)

C = _make_colours(sys.stdout.isatty())

# ---------------------------------------------------------------------------
# MP4 atom helpers
# ---------------------------------------------------------------------------

# The shwm atom inserted by the buggy version of tag_classical.py is always
# exactly 25 bytes, so every corrupted stco/co64 entry is short by this amount.
_SHWM_SIZE = 25


def _find_atom(data: bytes | bytearray, name: bytes, start: int, end: int) -> tuple[int, int]:
    """
    Walk sibling atoms in data[start:end] and return (offset, size) of the
    first atom whose 4-byte name matches *name*.  Returns (-1, -1) if absent.
    """
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset:offset + 4], 'big')
        if size < 8:
            break
        if bytes(data[offset + 4:offset + 8]) == name:
            return offset, size
        offset += size
    return -1, -1


def _walk_offsets(data: bytes | bytearray, moov_off: int, moov_size: int,
                  callback) -> None:
    """
    Recursively walk all stco and co64 atoms inside moov and call
    callback(atom_offset, entry_index, entry_byte_offset, entry_value)
    for every chunk-offset entry found.
    """
    containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}

    def walk(start: int, end: int) -> None:
        offset = start
        while offset + 8 <= end:
            size = int.from_bytes(data[offset:offset + 4], 'big')
            name = bytes(data[offset + 4:offset + 8])
            if size < 8:
                break
            if name == b'stco':
                count = int.from_bytes(data[offset + 12:offset + 16], 'big')
                for i in range(count):
                    p = offset + 16 + i * 4
                    val = int.from_bytes(data[p:p + 4], 'big')
                    callback(offset, i, p, val, 4)
            elif name == b'co64':
                count = int.from_bytes(data[offset + 12:offset + 16], 'big')
                for i in range(count):
                    p = offset + 16 + i * 8
                    val = int.from_bytes(data[p:p + 8], 'big')
                    callback(offset, i, p, val, 8)
            elif name in containers:
                walk(offset + 8, offset + size)
            offset += size

    walk(moov_off + 8, moov_off + moov_size)


def _min_chunk_offset(data: bytes | bytearray, moov_off: int,
                      moov_size: int) -> int | None:
    """
    Return the minimum stco/co64 value across all chunk-offset entries inside
    moov, or None if no such atoms exist.
    """
    minimum: int | None = None

    def cb(_atom_off, _idx, _byte_off, val, _width):
        nonlocal minimum
        if minimum is None or val < minimum:
            minimum = val

    _walk_offsets(data, moov_off, moov_size, cb)
    return minimum


def _adjust_chunk_offsets(data: bytearray, moov_off: int, delta: int) -> None:
    """Add *delta* to every stco/co64 entry inside moov."""
    moov_size = int.from_bytes(data[moov_off:moov_off + 4], 'big')

    def cb(_atom_off, _idx, byte_off, val, width):
        data[byte_off:byte_off + width] = (val + delta).to_bytes(width, 'big')

    _walk_offsets(data, moov_off, moov_size, cb)


# ---------------------------------------------------------------------------
# Corruption detection and repair
# ---------------------------------------------------------------------------

def _location_to_path(location: str) -> str:
    """Convert a file:// URL from Library.xml to a local filesystem path."""
    if location.startswith('file://'):
        return unquote(location[7:])
    return location


def corruption_status(path: str) -> tuple[bool, bool]:
    """
    Return (has_shwm, is_corrupted) for the file at *path*.

    has_shwm     — True if a shwm atom is present anywhere in the file.
    is_corrupted — True if mdat follows moov AND any stco/co64 entry points
                   to a byte position before the start of mdat (meaning the
                   offsets were never updated after moov was grown).

    Returns (False, False) on any I/O error.
    """
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError:
        return False, False

    has_shwm = b'shwm' in data

    moov_off, moov_size = _find_atom(data, b'moov', 0, len(data))
    if moov_off < 0 or moov_size < 8:
        return has_shwm, False

    mdat_off, _ = _find_atom(data, b'mdat', 0, len(data))
    if mdat_off < 0 or mdat_off <= moov_off + moov_size:
        # mdat precedes or abuts moov — insertion inside moov cannot have
        # shifted mdat, so no offset corruption is possible.
        return has_shwm, False

    min_off = _min_chunk_offset(data, moov_off, moov_size)
    if min_off is None:
        return has_shwm, False

    return has_shwm, min_off < mdat_off


def fix_chunk_offsets(path: str) -> None:
    """
    Repair stco/co64 corruption in the file at *path* by adding _SHWM_SIZE
    to every chunk-offset entry.  The file is replaced atomically.

    Raises:
        ValueError  – required atom structure not found.
        OSError     – file I/O error.
    """
    with open(path, 'rb') as fh:
        raw = fh.read()

    data = bytearray(raw)

    moov_off, _ = _find_atom(data, b'moov', 0, len(data))
    if moov_off < 0:
        raise ValueError(f"no moov atom found in {path!r}")

    _adjust_chunk_offsets(data, moov_off, _SHWM_SIZE)

    dir_ = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=dir_)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Library scan
# ---------------------------------------------------------------------------

def load_library(path: str) -> dict:
    with open(path, 'rb') as fh:
        return plistlib.load(fh)


def find_corrupted(library_path: str) -> tuple[list[tuple[dict, str]], int, int]:
    """
    Walk every File/Remote track in *library_path* and identify M4A/MP4 files
    that are corrupted.

    Returns:
        (corrupted, skipped_missing, skipped_type)
        corrupted       — list of (track_dict, file_path)
        skipped_missing — count of tracks whose file was not found on disk
        skipped_type    — count of tracks that are not M4A/MP4/M4P files
    """
    library = load_library(library_path)
    tracks  = library.get('Tracks', {})

    corrupted:       list[tuple[dict, str]] = []
    skipped_missing: int = 0
    skipped_type:    int = 0

    for _tid, track in tracks.items():
        if track.get('Track Type', '') not in ('File', 'Remote'):
            continue
        loc  = track.get('Location', '')
        path = _location_to_path(loc)
        if not path.lower().endswith(('.m4a', '.mp4', '.m4p')):
            skipped_type += 1
            continue
        if not os.path.exists(path):
            skipped_missing += 1
            continue
        _has_shwm, is_bad = corruption_status(path)
        if is_bad:
            corrupted.append((track, path))

    return corrupted, skipped_missing, skipped_type


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

def print_help() -> None:
    print(f"""\
{C['BOLD']}Usage:{C['RESET']}
    python3 fix_chunk_offsets.py --scan  [Library.xml]
    python3 fix_chunk_offsets.py --fix   [Library.xml]

{C['BOLD']}Description:{C['RESET']}
    Scans M4A/MP4 files for stco/co64 chunk-offset corruption introduced by
    an earlier version of tag_classical.py.

    That version inserted a {_SHWM_SIZE}-byte shwm atom inside the moov box (which
    precedes mdat in most files), growing moov by {_SHWM_SIZE} bytes and pushing mdat
    {_SHWM_SIZE} bytes later in the file.  The stco/co64 tables — which store absolute
    byte positions pointing into mdat — were not updated to reflect the shift.
    The audio decoder then reads from positions that are {_SHWM_SIZE} bytes too early,
    making the file unplayable.

    {C['BOLD']}Detection:{C['RESET']} a file is flagged when it contains a shwm atom AND at least
    one stco or co64 entry points before the start of the mdat atom.

    {C['BOLD']}Repair:{C['RESET']} add {_SHWM_SIZE} to every stco and co64 entry in the file.  Files are
    written atomically (temp file + rename) so a crash cannot leave a
    half-written file.

{C['BOLD']}Arguments:{C['RESET']}
    Library.xml     Path to an Apple Music Library XML export.
                    Defaults to Library.xml in the current directory,
                    then ~/Music/Music/Library.xml.
                    Export from Apple Music: File › Library › Export Library…

{C['BOLD']}Options:{C['RESET']}
    -h, --help      Show this help message and exit.
    --scan          Report corrupted files.  No files are modified.
    --fix           Report and repair corrupted files.
                    Exits with code 1 if any repair fails.

{C['BOLD']}Requirements:{C['RESET']}
    Python 3.10+, standard library only.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ('-h', '--help'):
        print_help()
        return

    if args[0] not in ('--scan', '--fix'):
        print(
            f"{C['RED']}Error:{C['RESET']} expected --scan or --fix as the first argument.\n"
            "Run with -h for usage.",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = args[0]
    args = args[1:]

    # Locate Library.xml
    if args:
        library_path = os.path.expanduser(args[0])
    else:
        candidates = [
            os.path.join(os.getcwd(), 'Library.xml'),
            os.path.expanduser('~/Music/Music/Library.xml'),
        ]
        library_path = next((p for p in candidates if os.path.exists(p)), None)
        if library_path is None:
            print(
                f"{C['RED']}Error:{C['RESET']} could not find Library.xml.\n"
                "Pass the path explicitly or export from Apple Music via "
                "File › Library › Export Library…",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Loading library: {library_path}")
    corrupted, skipped_missing, skipped_type = find_corrupted(library_path)

    if skipped_missing:
        print(f"{C['DIM']}  {skipped_missing} track(s) skipped — file not found on disk{C['RESET']}")

    if not corrupted:
        print(f"\n{C['GREEN']}No corrupted files found.{C['RESET']}  All done.")
        return

    print(f"\n{C['BOLD']}{len(corrupted)}{C['RESET']} corrupted file(s) found:\n")
    for track, path in corrupted:
        name  = track.get('Name', '?')
        album = track.get('Album', '')
        print(f"  {C['YELLOW']}⚠{C['RESET']}  {C['CYAN']}{name}{C['RESET']}")
        if album:
            print(f"       {C['DIM']}Album:{C['RESET']} {album}")
        print(f"       {C['DIM']} File:{C['RESET']} {path}")

    if mode == '--scan':
        print(
            f"\n{C['DIM']}Scan only — no files modified.  "
            f"Run with --fix to repair.{C['RESET']}"
        )
        return

    # --fix
    print(f"\nRepairing {len(corrupted)} file(s)…\n")
    ok   = 0
    fail = 0
    for track, path in corrupted:
        name = track.get('Name', '?')
        try:
            fix_chunk_offsets(path)
            print(f"  {C['GREEN']}✓{C['RESET']}  {name!r}")
            ok += 1
        except Exception as exc:
            print(f"  {C['RED']}✗{C['RESET']}  {name!r}: {exc}", file=sys.stderr)
            fail += 1

    print(f"\n{C['BOLD']}Done.{C['RESET']}  {ok} repaired, {fail} failed.")
    if fail:
        sys.exit(1)


if __name__ == '__main__':
    main()
