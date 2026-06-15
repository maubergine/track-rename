#!/usr/bin/env python3
"""
Apple Music Classical Tagger
-----------------------------
Sets Work, Grouping, Movement Name, Movement Number, Movement Count, and the
shwm (Show Work & Movement) atom on classical music tracks derived from their
existing titles.

A title must contain a colon to be processed:
  "Symphony No. 5 in C minor, Op. 67: II. Andante con moto"
   → Work / Grouping: "Symphony No. 5 in C minor, Op. 67"
   → Movement Name:   "Andante con moto"
   → Movement Number: 2
   → Movement Count:  <highest Roman numeral found across same-work tracks>
   → shwm atom:       1  (written directly into the MP4 file)

Within each album, all tracks sharing the same work prefix (the text before
the first colon) are considered part of the same work.  The movement count is
the highest Arabic value obtained by converting the leading Roman numeral of
any track in that group.

Usage:
    python3 tag_classical.py              # Library.xml in cwd or ~/Music/Music/Library.xml
    python3 tag_classical.py /path/to/Library.xml
    python3 tag_classical.py --scan       # report tracks missing shwm in their MP4 file
    python3 tag_classical.py --remediate  # add missing shwm atoms to those files

Requires: Python 3.10+, standard library only.
Tags are applied via AppleScript (macOS only).  Apple Music must be running.
The shwm atom is written directly to the MP4/M4A file on disk.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit(
        "Error: tag_classical.py requires Python 3.10 or later.\n"
        "You are running Python {}.{}.".format(*sys.version_info[:2])
    )

import os
import re
import plistlib
import subprocess
import tempfile
import json
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# Terminal colours (disabled when not a tty)
# ---------------------------------------------------------------------------

def _make_colours(enabled: bool):
    if enabled:
        return dict(
            GREEN='\033[32m', YELLOW='\033[33m', CYAN='\033[36m',
            RED='\033[31m', BOLD='\033[1m', RESET='\033[0m', DIM='\033[2m',
            MAGENTA='\033[35m',
        )
    return defaultdict(str)

C = _make_colours(sys.stdout.isatty())

# ---------------------------------------------------------------------------
# Roman-numeral helpers
# ---------------------------------------------------------------------------

def _roman(n: int) -> str:
    result, val = '', n
    for numeral, value in (
        ('C', 100), ('XC', 90), ('L', 50), ('XL', 40),
        ('X', 10),  ('IX', 9),  ('V', 5),  ('IV', 4), ('I', 1),
    ):
        while val >= value:
            result += numeral
            val -= value
    return result

_ROMAN_TO_INT: dict[str, int] = {_roman(n): n for n in range(1, 101)}

# Matches a leading Roman numeral token followed by a period, space, or end of string.
# Groups: either group 1 or group 2 holds the token.
_LEADING_ROMAN = re.compile(r'^([IVXLC]+)[.\s]|^([IVXLC]+)$')

# Matches a leading "No. N" prefix (e.g. "No. 1 in F: Gavotte").
_LEADING_NO = re.compile(r'^No\.\s+(\d+)\b', re.IGNORECASE)


def roman_to_int(numeral: str) -> int | None:
    """Convert a Roman numeral string to an integer, or None if not recognised."""
    return _ROMAN_TO_INT.get(numeral.upper())


def extract_leading_roman(text: str) -> tuple[str | None, int | None]:
    """
    If *text* begins with a valid Roman numeral (optionally followed by '.' or space),
    return (numeral_str, arabic_int).  Otherwise return (None, None).
    """
    m = _LEADING_ROMAN.match(text.strip())
    if not m:
        return None, None
    token = (m.group(1) or m.group(2)).upper()
    value = roman_to_int(token)
    if value is None:
        return None, None
    return token, value


# Roman numeral with a 1–2 character lowercase suffix, e.g. "IIa", "IIIb".
# Used for pieces whose movements are subdivided into lettered sub-sections.
_LEADING_ROMAN_LETTERED = re.compile(r'^([IVXLC]+[a-z]{1,2})[.\s]|^([IVXLC]+[a-z]{1,2})$')

# Trailing Roman numeral in movement name, e.g. "Mazurka I", "Variation III".
_TRAILING_ROMAN = re.compile(r'^(.+?)\s+([IVXLC]+)$')


def extract_leading_roman_lettered(text: str) -> tuple[str | None, int | None]:
    """
    If *text* begins with a Roman numeral immediately followed by 1–2 lowercase
    letters (e.g. "IIa Andante", "IIIb"), return (full_token, arabic_int) where
    the integer is the value of the Roman part only.  The letter suffix is kept
    in the token so it can be stripped from the movement name, but is not used
    for numbering — callers treat the whole work as sequentially numbered.
    Returns (None, None) if not recognised or the Roman part is invalid.
    """
    m = _LEADING_ROMAN_LETTERED.match(text.strip())
    if not m:
        return None, None
    token = m.group(1) or m.group(2)
    roman_part = token.rstrip('abcdefghijklmnopqrstuvwxyz').upper()
    value = roman_to_int(roman_part)
    if value is None:
        return None, None
    return token, value


def extract_trailing_roman(text: str) -> tuple[str | None, int | None]:
    """
    If *text* ends with a valid Roman numeral preceded by a space, return
    (stem, arabic_int) with the numeral stripped.  Otherwise return (None, None).
    """
    m = _TRAILING_ROMAN.match(text.strip())
    if not m:
        return None, None
    token = m.group(2).upper()
    value = roman_to_int(token)
    if value is None:
        return None, None
    return m.group(1).strip(), value


# ---------------------------------------------------------------------------
# MP4 / M4A file helpers — shwm atom
# ---------------------------------------------------------------------------
# The shwm (Show Work & Movement) atom is an iTunes-specific MP4 box that
# Apple Music looks for to decide whether to display the Work/Movement fields
# in the Now Playing UI.  It lives inside moov › udta › meta › ilst and
# carries a single-byte integer value of 1.
#
# Atom layout (25 bytes total):
#   [4] size = 0x00000019 (25)
#   [4] name = "shwm"
#   [4] data sub-atom size = 0x00000011 (17)
#   [4] data sub-atom name = "data"
#   [4] type flag = 0x00000015 (21 = well-known integer)
#   [4] locale = 0x00000000
#   [1] value = 0x01
#
# Reference: ISO 14496-12 (MP4 base spec) §8; iTunes Metadata Format Spec.

_SHWM_ATOM = bytes([
    0x00, 0x00, 0x00, 0x19,  # size = 25
    0x73, 0x68, 0x77, 0x6d,  # 'shwm'
    0x00, 0x00, 0x00, 0x11,  # data sub-atom size = 17
    0x64, 0x61, 0x74, 0x61,  # 'data'
    0x00, 0x00, 0x00, 0x15,  # type = 21 (integer, well-known type)
    0x00, 0x00, 0x00, 0x00,  # locale = 0
    0x01,                    # value = 1
])


def _location_to_path(location: str) -> str:
    """Convert a file:// URL from Library.xml to a local filesystem path."""
    if location.startswith('file://'):
        return unquote(location[7:])
    return location


def _find_atom(data: bytes | bytearray, name: bytes, start: int, end: int) -> tuple[int, int]:
    """
    Walk sibling atoms in *data[start:end]* and return (offset, size) of the
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


def has_shwm(path: str) -> bool:
    """Return True if the MP4/M4A file at *path* already contains a shwm atom."""
    try:
        with open(path, 'rb') as fh:
            # shwm lives inside moov which is always near the start
            chunk = fh.read(10 * 1024 * 1024)
        return b'shwm' in chunk
    except OSError:
        return False


def _adjust_chunk_offsets(data: bytearray, moov_off: int, delta: int) -> None:
    """
    Add *delta* to every chunk-offset entry in every ``stco`` / ``co64`` atom
    found anywhere inside ``moov``.

    ``stco`` and ``co64`` store *absolute* byte positions pointing into the
    file's ``mdat`` payload.  When bytes are inserted inside ``moov`` before
    ``mdat``, ``mdat`` shifts by *delta* bytes and every stored offset must be
    updated to compensate.

    The walk is recursive through container atoms so it handles files with
    multiple tracks or alternate-data-ref structures.
    """
    moov_size = int.from_bytes(data[moov_off:moov_off + 4], 'big')
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
                    data[p:p + 4] = (val + delta).to_bytes(4, 'big')
            elif name == b'co64':
                count = int.from_bytes(data[offset + 12:offset + 16], 'big')
                for i in range(count):
                    p = offset + 16 + i * 8
                    val = int.from_bytes(data[p:p + 8], 'big')
                    data[p:p + 8] = (val + delta).to_bytes(8, 'big')
            elif name in containers:
                walk(offset + 8, offset + size)
            offset += size

    walk(moov_off + 8, moov_off + moov_size)


def write_shwm_to_mp4(path: str) -> None:
    """
    Insert a ``shwm=1`` atom into the MP4/M4A file at *path*.

    The atom is appended to the end of the ``ilst`` box inside
    ``moov › udta › meta``.  All ancestor atom size fields are updated in
    place.  When ``mdat`` follows ``moov``, all ``stco``/``co64`` chunk-offset
    entries are also incremented by the insertion size so that absolute audio
    data pointers remain correct.  The file is replaced atomically via a
    temporary file in the same directory.

    Raises:
        ValueError  – required atom structure not found (not a tagged M4A).
        OSError     – file I/O error.
    """
    with open(path, 'rb') as fh:
        raw = fh.read()

    if b'shwm' in raw:
        return  # already present — nothing to do

    data = bytearray(raw)

    moov_off, moov_size = _find_atom(data, b'moov', 0, len(data))
    if moov_off < 0:
        raise ValueError(f"no moov atom found in {path!r}")

    udta_off, udta_size = _find_atom(data, b'udta', moov_off + 8, moov_off + moov_size)
    if udta_off < 0:
        raise ValueError(f"no udta atom found in {path!r}")

    meta_off, meta_size = _find_atom(data, b'meta', udta_off + 8, udta_off + udta_size)
    if meta_off < 0:
        raise ValueError(f"no meta atom found in {path!r}")

    # meta has a 4-byte version/flags field before its child atoms
    ilst_off, ilst_size = _find_atom(data, b'ilst', meta_off + 12, meta_off + meta_size)
    if ilst_off < 0:
        raise ValueError(f"no ilst atom found in {path!r}")

    # Insert shwm at the end of ilst
    insert_pos = ilst_off + ilst_size
    delta = len(_SHWM_ATOM)
    data[insert_pos:insert_pos] = _SHWM_ATOM

    # Propagate the size increase up through all ancestor atoms
    for off in (ilst_off, meta_off, udta_off, moov_off):
        cur = int.from_bytes(data[off:off + 4], 'big')
        data[off:off + 4] = (cur + delta).to_bytes(4, 'big')

    # If mdat follows moov, the insertion has shifted mdat's position by
    # *delta* bytes.  stco/co64 hold absolute file offsets into mdat, so
    # every entry must be incremented to match the new position.
    mdat_off, _ = _find_atom(data, b'mdat', 0, len(data))
    if mdat_off > moov_off:
        _adjust_chunk_offsets(data, moov_off, delta)

    # Write atomically: temp file in the same directory → os.replace
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
# Library parsing
# ---------------------------------------------------------------------------

def load_library(path: str) -> dict:
    with open(path, 'rb') as fh:
        return plistlib.load(fh)


def group_tracks_by_album(tracks_dict: dict) -> dict:
    """Group File/Remote tracks by (album, artist)."""
    groups: dict = defaultdict(list)
    for _tid, track in tracks_dict.items():
        if track.get('Track Type', '') not in ('File', 'Remote'):
            continue
        album  = track.get('Album', '')
        artist = track.get('Artist') or track.get('Album Artist', '')
        groups[(album, artist)].append(track)
    return groups


# ---------------------------------------------------------------------------
# Tag proposal
# ---------------------------------------------------------------------------

@dataclass
class TagChange:
    track:           dict
    title:           str        # original track title (for display)
    work:            str        # proposed Work tag
    movement_name:   str        # proposed Movement Name tag
    movement_number: int | None # proposed Movement Number (arabic), or None
    movement_count:  int | None # proposed Movement Count, or None


def find_tags_for_album(
    tracks: list,
    split_level: int = 0,
    skip_numbered: bool = True,
    keep_roman: bool = False,
    lettered: bool = False,
) -> list[TagChange]:
    """
    Propose tags for *tracks* (a single album group) using *split_level* to
    choose the work/movement boundary.

    *split_level* is zero-indexed: 0 splits on the first colon (default),
    1 on the second, etc.  At level N the Nth colon-delimited segment becomes
    the Work and everything after it the raw movement name.  Tracks whose
    title has fewer than *split_level* + 2 segments are skipped at that level.
    """
    ordered = sorted(
        tracks,
        key=lambda t: (t.get('Disc Number', 1), t.get('Track Number', 0)),
    )

    # --- Pass 1: parse each track title ----------------------------------
    parsed: list[tuple] = []
    for track in ordered:
        title = track.get('Name', '').strip()
        parts = [p.strip() for p in title.split(':')]
        if len(parts) < split_level + 2:
            continue
        work          = ': '.join(parts[:split_level + 1])
        movement_name = ': '.join(parts[split_level + 1:]).strip()
        roman_str, arabic_int = extract_leading_roman(movement_name)
        if roman_str is not None:
            if not keep_roman:
                tail = movement_name[len(roman_str):]
                movement_name = tail.lstrip('. ').strip()
        elif lettered:
            roman_str, arabic_int = extract_leading_roman_lettered(movement_name)
            if roman_str is not None:
                if not keep_roman:
                    tail = movement_name[len(roman_str):]
                    movement_name = tail.lstrip('. ').strip()
            else:
                _, arabic_int = extract_trailing_roman(movement_name)
        else:
            _, arabic_int = extract_trailing_roman(movement_name)
        parsed.append((track, title, work, movement_name, roman_str, arabic_int))

    if not parsed:
        return []

    # --- Pass 2: compute movement count per work -------------------------
    work_max: dict[str, int] = defaultdict(int)
    for _track, _title, work, _mvt_name, _roman, arabic_int in parsed:
        if arabic_int is not None:
            work_max[work] = max(work_max[work], arabic_int)

    work_no_style: set[str] = set()
    for _track, _title, work, movement_name, _roman, arabic_int in parsed:
        if arabic_int is None and _LEADING_NO.match(movement_name):
            work_no_style.add(work)
    # Works where any track has a lettered Roman prefix (IIa, IIb …) use
    # sequential numbering — the letter suffix has no integer representation.
    for _track, _title, work, _mvt_name, roman_str, _arabic in parsed:
        if roman_str is not None and any(c.islower() for c in roman_str):
            work_no_style.add(work)
    work_no_total: dict[str, int] = {
        work: sum(1 for _, _, w, _, _, _ in parsed if w == work)
        for work in work_no_style
    }
    work_no_counter: dict[str, int] = defaultdict(int)

    # --- Pass 3: build TagChange list ------------------------------------
    album_changes: list[TagChange] = []
    for track, title, work, movement_name, _roman_str, arabic_int in parsed:
        if work in work_no_style:
            work_no_counter[work] += 1
            movement_number = work_no_counter[work]
            movement_count  = work_no_total[work]
        else:
            movement_number = arabic_int
            movement_count  = work_max.get(work) if arabic_int is not None else None

        if skip_numbered and track.get('Movement Number', 0) and track.get('Movement Count', 0):
            continue

        if (
            track.get('Work', '') == work
            and track.get('Movement Name', '') == movement_name
            and track.get('Movement Number', 0) == (movement_number or 0)
            and track.get('Movement Count', 0)  == (movement_count  or 0)
        ):
            continue

        album_changes.append(TagChange(
            track=track,
            title=title,
            work=work,
            movement_name=movement_name,
            movement_number=movement_number,
            movement_count=movement_count,
        ))

    return album_changes


def find_tags(tracks_by_album: dict, skip_numbered: bool = True) -> dict:
    """
    Walk each album group and propose tags at grouping level 0 (first colon).

    Returns:
        dict mapping (album, artist) -> list[TagChange]
    """
    result = {}
    for album_key, tracks in tracks_by_album.items():
        album_changes = find_tags_for_album(tracks, split_level=0, skip_numbered=skip_numbered)
        if album_changes:
            result[album_key] = album_changes
    return result


# ---------------------------------------------------------------------------
# Work-name override
# ---------------------------------------------------------------------------

def apply_work_override(
    changes: list[TagChange],
    custom_work: str,
    work_filter: str | None = None,
    keep_roman: bool = False,
    lettered: bool = False,
) -> list[TagChange]:
    """
    Re-tag tracks using *custom_work* as the Work tag.

    Tracks whose ``.work`` matches *work_filter* (or all tracks when
    *work_filter* is ``None``) are reprocessed: the custom work prefix is
    stripped from the title, any leading Roman numeral is removed from the
    resulting movement name (unless *keep_roman* is True), and sequential
    movement numbers (1 … n) are assigned in the order the matching tracks
    appear in *changes*.

    Non-matching tracks are returned unchanged.
    """
    # Pass 1: apply the new work name and derive movement names for matching tracks.
    target_indices = [
        i for i, c in enumerate(changes)
        if work_filter is None or c.work == work_filter
    ]
    prefix = custom_work + ':'
    result = list(changes)

    for idx in target_indices:
        change = changes[idx]
        title  = change.title

        if title.startswith(prefix):
            movement_name = title[len(prefix):].strip()
        else:
            movement_name = title.strip()

        roman_str, _ = extract_leading_roman(movement_name)
        if roman_str is not None:
            if not keep_roman:
                tail = movement_name[len(roman_str):]
                movement_name = tail.lstrip('. ').strip()
        elif lettered:
            roman_str, _ = extract_leading_roman_lettered(movement_name)
            if roman_str is not None and not keep_roman:
                tail = movement_name[len(roman_str):]
                movement_name = tail.lstrip('. ').strip()

        result[idx] = TagChange(
            track=change.track,
            title=title,
            work=custom_work,
            movement_name=movement_name,
            movement_number=None,  # assigned in pass 2
            movement_count=None,
        )

    # Pass 2: renumber ALL tracks sharing custom_work across the full change
    # list, in disc/track order.  This handles the case where multiple original
    # works have been merged into the same custom work name via repeated overrides.
    shared_indices = sorted(
        [i for i, c in enumerate(result) if c.work == custom_work],
        key=lambda i: (
            result[i].track.get('Disc Number', 1),
            result[i].track.get('Track Number', 0),
        ),
    )
    total = len(shared_indices)
    for seq, idx in enumerate(shared_indices, 1):
        c = result[idx]
        result[idx] = TagChange(
            track=c.track,
            title=c.title,
            work=c.work,
            movement_name=c.movement_name,
            movement_number=seq,
            movement_count=total,
        )

    return result


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_TAG_W = 14  # label column width for tag rows


def _tag_row(label: str, value: str, colour: str = '') -> str:
    reset = C['RESET'] if colour else ''
    return f"  {C['DIM']}{label:>{_TAG_W}}:{C['RESET']} {colour}{value}{reset}"


def display_album(album_key: tuple, changes: list[TagChange]) -> None:
    album, artist = album_key
    disc_nums = sorted({c.track.get('Disc Number', 1) for c in changes})
    multi_disc = len(disc_nums) > 1

    print(f"\n{C['BOLD']}Album:{C['RESET']}  {album}")
    print(f"{C['BOLD']}Artist:{C['RESET']} {artist}")
    if multi_disc:
        print(f"{C['BOLD']}Discs:{C['RESET']}  {', '.join(map(str, disc_nums))}")
    print(f"\n  {C['DIM']}{'Tr':>3}  {'Disc':>4}  Title / Proposed Tags{C['RESET']}")
    print(f"  {'─' * 76}")

    for change in changes:
        tr  = change.track.get('Track Number', '?')
        dis = change.track.get('Disc Number', 1)

        disc_str = f"  {C['DIM']}{str(dis):>4}{C['RESET']}" if multi_disc else f"  {C['DIM']}    {C['RESET']}"

        print(f"  {C['CYAN']}{str(tr):>3}{C['RESET']}{disc_str}  {change.title}")
        print(_tag_row('Work', change.work, C['GREEN']))
        print(_tag_row('Movement Name', change.movement_name, C['GREEN']))

        if change.movement_number is not None and change.movement_count is not None:
            mvt_str = f"{change.movement_number} / {change.movement_count}"
        elif change.movement_number is not None:
            mvt_str = f"{change.movement_number} / ?"
        else:
            mvt_str = '—'
        print(_tag_row('Movement No.', mvt_str, C['MAGENTA']))
        print()

    print()


# ---------------------------------------------------------------------------
# Interactive checkbox selector
# ---------------------------------------------------------------------------

def checkbox_select(changes: list) -> list | None:
    """
    Present *changes* (list of TagChange) as a numbered checklist (all checked
    by default).  The user toggles items by number, then confirms.

    Returns the selected subset (may be empty), or None if the user goes back.
    """
    selected = set(range(len(changes)))

    while True:
        print()
        for i, change in enumerate(changes):
            tr   = change.track.get('Track Number', '?')
            dis  = change.track.get('Disc Number', 1)
            mark = f"{C['GREEN']}[x]{C['RESET']}" if i in selected else f"{C['DIM']}[ ]{C['RESET']}"
            if change.movement_number is not None and change.movement_count is not None:
                mvt_summary = f"mvt {change.movement_number}/{change.movement_count}"
            elif change.movement_number is not None:
                mvt_summary = f"mvt {change.movement_number}"
            else:
                mvt_summary = ''
            summary = f"{change.work}" + (f"  ({mvt_summary})" if mvt_summary else '')
            print(f"  {mark} {C['DIM']}{i+1:>2}.{C['RESET']}  "
                  f"{C['CYAN']}Tr {str(tr):>3}  Disc {dis}{C['RESET']}  {change.title}")
            print(f"              {C['DIM']}→{C['RESET']}  {C['GREEN']}{summary}{C['RESET']}")

        n = len(selected)
        total = len(changes)
        print(f"\n  {n}/{total} selected")
        try:
            raw = input(
                "  Toggle (e.g. 1 3), [a]ll, [n]one, [c]onfirm, [b]ack  > "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw == 'c' or raw == '':
            return [changes[i] for i in sorted(selected)]
        if raw == 'b':
            return None
        if raw == 'a':
            selected = set(range(len(changes)))
        elif raw == 'n':
            selected = set()
        else:
            for tok in raw.replace(',', ' ').split():
                try:
                    idx = int(tok) - 1
                    if 0 <= idx < len(changes):
                        if idx in selected:
                            selected.discard(idx)
                        else:
                            selected.add(idx)
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# AppleScript tag application
# ---------------------------------------------------------------------------

def _as_str(s: str) -> str:
    """Escape a Python string for use inside an AppleScript double-quoted string."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _as_int(val: int | None, default: int = 0) -> int:
    return val if val is not None else default


def tag_via_applescript(tag_ops: list[tuple]) -> bool:
    """
    Apply tag operations to Apple Music via a single osascript call.

    *tag_ops* is a list of
        (persistent_id, work, movement_name, movement_number, movement_count)
    where movement_number and movement_count are ints (0 means unset).
    Grouping is set to the same value as work.
    """
    if not tag_ops:
        return True

    # Build the AppleScript list literal.
    entries = ', '.join(
        f'{{"{_as_str(pid)}", "{_as_str(work)}", "{_as_str(mvt_name)}",'
        f' {mvt_num}, {mvt_cnt}}}'
        for pid, work, mvt_name, mvt_num, mvt_cnt in tag_ops
    )

    script = f'''\
tell application "Music"
    set tagList to {{{entries}}}
    repeat with entry in tagList
        set theID      to item 1 of entry
        set theWork    to item 2 of entry
        set theMvtName to item 3 of entry
        set theMvtNum  to item 4 of entry
        set theMvtCnt  to item 5 of entry
        try
            set theTrack to first file track of library playlist 1 ¬
                whose persistent ID is theID
            set work             of theTrack to theWork
            set grouping         of theTrack to theWork
            set movement         of theTrack to theMvtName
            set movement number  of theTrack to theMvtNum
            set movement count   of theTrack to theMvtCnt
        on error errMsg
            log "Could not tag " & theID & ": " & errMsg
        end try
    end repeat
end tell
'''

    with tempfile.NamedTemporaryFile(mode='w', suffix='.applescript', delete=False) as fh:
        fh.write(script)
        tmp = fh.name

    try:
        res = subprocess.run(
            ['osascript', tmp],
            capture_output=True, text=True, timeout=600,
        )
        if res.returncode != 0:
            print(f"{C['RED']}AppleScript error:{C['RESET']} {res.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"{C['RED']}AppleScript timed out.{C['RESET']}", file=sys.stderr)
        return False
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Interactive approval loop
# ---------------------------------------------------------------------------

def _input_prefilled(prompt: str, default: str) -> str:
    """Prompt showing *default* in brackets; Enter alone accepts it."""
    result = input(f"{prompt}[{default}]  > ").strip()
    return result if result else default

def prompt(msg: str) -> str:
    """Read a single-character response."""
    try:
        raw = input(msg).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 'q'
    return raw[:1] if raw else ''


def approval_loop(
    tags_by_album: dict,
    tracks_by_album: dict | None = None,
    skip_numbered: bool = True,
) -> tuple[list[tuple], list[tuple[str, str]]]:
    """
    Present each album's proposed tag changes to the user and collect approvals.

    Returns a pair (approved, newly_ignored) where:
        approved       — flat list of (pid, work, mvt_name, mvt_num, mvt_cnt, file_path)
        newly_ignored  — list of (album, artist) tuples the user chose to ignore
    """
    approved: list[tuple] = []
    newly_ignored: list[tuple[str, str]] = []
    auto_approve = False

    albums = sorted(
        tags_by_album.items(),
        key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()),
    )
    total = len(albums)

    for idx, (album_key, changes) in enumerate(albums, 1):
        current_level = 0
        keep_roman    = False
        lettered      = False
        album_tracks  = (tracks_by_album or {}).get(album_key, [])
        max_colons    = max((t.get('Name', '').count(':') for t in album_tracks), default=0)
        max_level     = max(max_colons - 1, 0)

        display_album(album_key, changes)

        if auto_approve:
            _collect(approved, changes)
            continue

        n = len(changes)
        print(f"  Album {idx}/{total}  —  {n} track(s) to tag")

        while True:
            resp = prompt(
                "  Apply? [y]es / [n]o / [s]elect / [o]verride work / [g]rouping / [k]eep Roman / [l]ettered / [i]gnore / [a]ll remaining / [q]uit  > "
            )
            if resp == 'y':
                _collect(approved, changes)
                break
            elif resp == 'n':
                print(f"  {C['DIM']}Skipped.{C['RESET']}")
                break
            elif resp == 'i':
                newly_ignored.append(album_key)
                print(f"  {C['DIM']}Ignored — won't appear in future runs.{C['RESET']}")
                break
            elif resp == 's':
                subset = checkbox_select(changes)
                if subset is None:
                    pass  # back — re-show album prompt
                elif not subset:
                    print(f"  {C['DIM']}Nothing selected — skipped.{C['RESET']}")
                    break
                else:
                    _collect(approved, subset)
                    break
            elif resp == 'o':
                works = list(dict.fromkeys(c.work for c in changes))
                if len(works) == 1:
                    work_to_override = works[0]
                else:
                    print(f"\n  {len(works)} works in this album:")
                    for wi, w in enumerate(works, 1):
                        n_w = sum(1 for c in changes if c.work == w)
                        print(f"    {wi}. {w}  ({n_w} track(s))")
                    try:
                        raw = input(
                            "  Which work to override? (number, [a]ll, Enter to cancel)  > "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                    if not raw:
                        continue
                    if raw == 'a':
                        work_to_override = None  # all works
                    else:
                        try:
                            wi = int(raw) - 1
                        except ValueError:
                            print("  Invalid selection.")
                            continue
                        if not (0 <= wi < len(works)):
                            print("  Invalid selection.")
                            continue
                        work_to_override = works[wi]

                default_name = work_to_override if work_to_override is not None else works[0]
                try:
                    new_work = _input_prefilled("  Work name  > ", default_name).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if not new_work:
                    print(f"  {C['DIM']}Cancelled.{C['RESET']}")
                    continue

                changes = apply_work_override(
                    changes, new_work, work_filter=work_to_override,
                    keep_roman=keep_roman, lettered=lettered,
                )
                display_album(album_key, changes)
                print(f"  Album {idx}/{total}  —  {len(changes)} track(s) to tag")
            elif resp == 'g':
                if max_level == 0:
                    print(f"  {C['DIM']}No sub-groupings available (no track has 2+ colons).{C['RESET']}")
                else:
                    current_level = (current_level + 1) % (max_level + 1)
                    new_changes = find_tags_for_album(
                        album_tracks, split_level=current_level,
                        skip_numbered=skip_numbered, keep_roman=keep_roman,
                        lettered=lettered,
                    )
                    level_label = (
                        f"level {current_level} — Work = segments 1–{current_level + 1}"
                        if current_level > 0
                        else "level 0 — default (1st colon)"
                    )
                    if not new_changes:
                        print(
                            f"  {C['DIM']}Grouping {level_label}: "
                            f"no tracks qualify at this level.{C['RESET']}"
                        )
                    else:
                        changes = new_changes
                        display_album(album_key, changes)
                        print(f"  {C['DIM']}Grouping {level_label}{C['RESET']}")
                        print(f"  Album {idx}/{total}  —  {len(changes)} track(s) to tag")
            elif resp == 'k':
                keep_roman = not keep_roman
                new_changes = find_tags_for_album(
                    album_tracks, split_level=current_level,
                    skip_numbered=skip_numbered, keep_roman=keep_roman,
                    lettered=lettered,
                )
                state_label = (
                    f"{C['CYAN']}on{C['RESET']}  — numerals retained in movement names"
                    if keep_roman
                    else f"{C['DIM']}off{C['RESET']} — numerals stripped from movement names"
                )
                if not new_changes:
                    print(f"  Keep Roman: {state_label}")
                    print(f"  {C['DIM']}No tracks to tag at this setting.{C['RESET']}")
                else:
                    changes = new_changes
                    display_album(album_key, changes)
                    print(f"  Keep Roman: {state_label}")
                    print(f"  Album {idx}/{total}  —  {len(changes)} track(s) to tag")
            elif resp == 'l':
                lettered = not lettered
                new_changes = find_tags_for_album(
                    album_tracks, split_level=current_level,
                    skip_numbered=skip_numbered, keep_roman=keep_roman,
                    lettered=lettered,
                )
                state_label = (
                    f"{C['CYAN']}on{C['RESET']}  — Roman+letter prefixes recognised (IIa, IIb …)"
                    if lettered
                    else f"{C['DIM']}off{C['RESET']} — only pure Roman numerals recognised"
                )
                if not new_changes:
                    print(f"  Lettered Roman: {state_label}")
                    print(f"  {C['DIM']}No tracks to tag at this setting.{C['RESET']}")
                else:
                    changes = new_changes
                    display_album(album_key, changes)
                    print(f"  Lettered Roman: {state_label}")
                    print(f"  Album {idx}/{total}  —  {len(changes)} track(s) to tag")
            elif resp == 'a':
                auto_approve = True
                _collect(approved, changes)
                break
            elif resp == 'q':
                print(
                    f"\n{C['YELLOW']}Quit — {len(approved)} tag operation(s) "
                    f"already approved will be applied.{C['RESET']}"
                )
                return approved, newly_ignored
            else:
                print("  Please enter  y / n / s / o / g / k / l / i / a / q")

    return approved, newly_ignored


def _collect(approved: list, changes: list[TagChange]) -> None:
    """Append approved tag ops from *changes* to *approved*."""
    for change in changes:
        pid = change.track.get('Persistent ID')
        if not pid:
            continue
        file_path = _location_to_path(change.track.get('Location', ''))
        approved.append((
            pid,
            change.work,
            change.movement_name,
            _as_int(change.movement_number),
            _as_int(change.movement_count),
            file_path,
        ))


# ---------------------------------------------------------------------------
# Ignore-list helpers
# ---------------------------------------------------------------------------

def load_ignores(path: str) -> set[tuple[str, str]]:
    """Load ignored (album, artist) pairs from *path*. Returns empty set if file absent."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return {
            (entry['album'], entry['artist'])
            for entry in data
            if 'album' in entry and 'artist' in entry
        }
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(
            f"{C['YELLOW']}Warning:{C['RESET']} Could not read ignores file {path!r}: {exc}",
            file=sys.stderr,
        )
        return set()


def save_ignores(path: str, ignores: set[tuple[str, str]]) -> None:
    """Write *ignores* to *path* as a sorted JSON array."""
    data = sorted(
        [{'album': album, 'artist': artist} for album, artist in ignores],
        key=lambda e: (e['album'].lower(), e['artist'].lower()),
    )
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

def print_help() -> None:
    print(f"""\
{C['BOLD']}Usage:{C['RESET']}
    python3 tag_classical.py [options] [Library.xml]

{C['BOLD']}Description:{C['RESET']}
    Set Work, Grouping, Movement Name, Movement Number, Movement Count, and
    the shwm (Show Work & Movement) atom on classical music tracks in Apple
    Music.

    Tracks whose title contains a colon are parsed as:
        "Work Name: [Roman numeral] Movement Name"

    Tags derived from that split:
        {C['GREEN']}Work / Grouping{C['RESET']}  everything before the first colon
        {C['GREEN']}Movement Name{C['RESET']}    remainder after stripping the leading Roman numeral
        {C['MAGENTA']}Movement Number{C['RESET']}  Arabic value of the leading Roman numeral
        {C['MAGENTA']}Movement Count{C['RESET']}   highest movement number seen within the same work
        {C['CYAN']}shwm atom{C['RESET']}        integer 1, written directly into the MP4/M4A file

    Tracks whose tags already match the proposed values are skipped.

{C['BOLD']}Arguments:{C['RESET']}
    Library.xml         Path to an Apple Music Library XML export.
                        Defaults to Library.xml in the current directory,
                        then ~/Music/Music/Library.xml.
                        Export from Apple Music: File › Library › Export Library…

{C['BOLD']}Options:{C['RESET']}
    -h, --help          Show this help message and exit.
    --scan              Report tracks that have Work or Movement Name set in
                        the library XML but are missing the shwm atom in their
                        MP4/M4A file.  No files are modified.
    --remediate         Like --scan, but also writes the shwm atom to each
                        affected file.  Exits with code 1 if any write fails.

    --include-numbered  Include tracks that already have both Movement Number
                        and Movement Count set in the library.  By default
                        those tracks are skipped on the assumption that they
                        have already been tagged.

    --no-ignores        Do not load or update the ignore list.  All albums are
                        shown regardless of previous ignore decisions.
    --ignores-file PATH Use PATH instead of the default ./ignores.json.

    --scan and --remediate accept an optional Library.xml path as their next
    argument (same search order as the default mode when omitted).

{C['BOLD']}Approval loop:{C['RESET']}
    Albums are shown one at a time.  For each album:
        {C['CYAN']}[y]es{C['RESET']}            Apply all proposed tags.
        {C['CYAN']}[n]o{C['RESET']}             Skip this album.
        {C['CYAN']}[s]elect{C['RESET']}         Choose individual tracks via a numbered checklist.
        {C['CYAN']}[o]verride work{C['RESET']}  Enter a custom Work tag for a group of tracks.
                         Sequential movement numbers (1 … n) are assigned and
                         the work prefix is stripped from each movement name.
                         Use this for pieces with irregular subtitle structures
                         (e.g. "BWV 194: Seconda Parte (Post concionem)").
        {C['CYAN']}[g]rouping{C['RESET']}       Cycle through grouping levels.  Level 0 (default)
                         splits on the first colon; level 1 on the second, etc.
                         Useful for multi-level titles such as
                         "Tafelmusik Part I: Ouverture in E minor: I Ouverture"
                         where level 1 produces Work =
                         "Tafelmusik Part I: Ouverture in E minor".
        {C['CYAN']}[k]eep Roman{C['RESET']}    Toggle whether leading Roman numerals are retained
                         in the Movement Name tag.  Off by default (numerals are
                         stripped).  Toggle on when the numeral is meaningful
                         context, e.g. "IIa Andante" → "IIa Andante" rather
                         than "Andante".
        {C['CYAN']}[l]ettered{C['RESET']}      Toggle recognition of Roman+letter prefixes such as
                         IIa, IIb, IIc.  Off by default to avoid false matches
                         on ordinary words (In, Il, Via …).  When on, any work
                         containing a lettered track is numbered sequentially.
        {C['CYAN']}[i]gnore{C['RESET']}         Permanently skip this album on future runs.
                         Recorded in ignores.json (see --ignores-file).
        {C['CYAN']}[a]ll remaining{C['RESET']}  Apply all remaining albums without further prompting.
        {C['CYAN']}[q]uit{C['RESET']}           Stop reviewing; apply tags approved so far.

{C['BOLD']}Requirements:{C['RESET']}
    macOS, Python 3.10+, Apple Music running (for the AppleScript tag step).
    Files must be accessible on disk for the shwm atom write step.
""")


# ---------------------------------------------------------------------------
# shwm scan / remediate
# ---------------------------------------------------------------------------

def scan_shwm(library_path: str, mode: int) -> None:
    """
    Scan the library for MP4/M4A tracks that have Work or Movement Name tags
    set in the XML but are missing the shwm atom in their file.

    mode 1 — report only (no file modifications)
    mode 2 — remediate: write the shwm atom to each affected file
    """
    print(f"Loading library: {library_path}")
    library   = load_library(library_path)
    tracks    = library.get('Tracks', {})
    print(f"  {len(tracks):,} tracks found\n")

    candidates: list[tuple[dict, str]] = []
    skipped_missing = 0
    skipped_type    = 0

    for _tid, track in tracks.items():
        if not (track.get('Work', '').strip() or track.get('Movement Name', '').strip()):
            continue
        loc  = track.get('Location', '')
        path = _location_to_path(loc)
        if not path.lower().endswith(('.m4a', '.mp4', '.m4p')):
            skipped_type += 1
            continue
        if not os.path.exists(path):
            skipped_missing += 1
            continue
        if not has_shwm(path):
            candidates.append((track, path))

    if skipped_missing:
        print(f"{C['DIM']}  {skipped_missing} track(s) skipped — file not found on disk{C['RESET']}")
    if skipped_type:
        print(f"{C['DIM']}  {skipped_type} track(s) skipped — not an MP4/M4A file{C['RESET']}")

    if not candidates:
        print("All qualifying tracks already have the shwm atom.  Nothing to do.")
        return

    if mode == 1:
        # Report only
        print(
            f"\n{C['BOLD']}{len(candidates)}{C['RESET']} track(s) have Work/Movement in the "
            f"library XML but are missing the shwm atom:\n"
        )
        for track, path in candidates:
            name  = track.get('Name', '?')
            work  = track.get('Work', '')
            album = track.get('Album', '')
            print(f"  {C['CYAN']}{name}{C['RESET']}")
            if work:
                print(f"    Work:  {work}")
            if album:
                print(f"    Album: {album}")
            print(f"    File:  {path}")
        return

    # mode 2 — remediate
    print(f"\nWriting shwm atom to {len(candidates)} file(s)…\n")
    ok   = 0
    fail = 0
    for track, path in candidates:
        name = track.get('Name', '?')
        try:
            write_shwm_to_mp4(path)
            print(f"  {C['GREEN']}✓{C['RESET']}  {name!r}")
            ok += 1
        except Exception as exc:
            print(
                f"  {C['RED']}✗{C['RESET']}  {name!r}: {exc}",
                file=sys.stderr,
            )
            fail += 1

    print(f"\n{C['BOLD']}Done.{C['RESET']}  {ok} succeeded, {fail} failed.")
    if fail:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CHUNK = 50  # tag operations per osascript call


def main():
    # --- Parse arguments -----------------------------------------------------
    args = sys.argv[1:]

    if args and args[0] in ('-h', '--help'):
        print_help()
        return

    scan_mode: int | None = None

    if args and args[0] in ('--scan', '--remediate'):
        scan_mode = 1 if args[0] == '--scan' else 2
        args = args[1:]

    include_numbered = '--include-numbered' in args
    if include_numbered:
        args.remove('--include-numbered')

    use_ignores = '--no-ignores' not in args
    if not use_ignores:
        args.remove('--no-ignores')

    ignores_file = 'ignores.json'
    if '--ignores-file' in args:
        idx = args.index('--ignores-file')
        if idx + 1 < len(args):
            ignores_file = args.pop(idx + 1)
            args.pop(idx)
        else:
            print(
                f"{C['RED']}Error:{C['RESET']} --ignores-file requires a PATH argument.",
                file=sys.stderr,
            )
            sys.exit(1)

    # --- Locate library XML --------------------------------------------------
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
                f"{C['RED']}Error:{C['RESET']} Could not find Library.xml.\n"
                "Pass the path explicitly:  python3 tag_classical.py /path/to/Library.xml\n\n"
                "To export from Apple Music: File › Library › Export Library…",
                file=sys.stderr,
            )
            sys.exit(1)

    # --- scan / remediate mode -----------------------------------------------
    if scan_mode is not None:
        scan_shwm(library_path, scan_mode)
        return

    # --- Normal tagging mode -------------------------------------------------
    print(f"Loading library: {library_path}")
    library      = load_library(library_path)
    tracks_dict  = library.get('Tracks', {})
    print(f"  {len(tracks_dict):,} tracks found")

    groups = group_tracks_by_album(tracks_dict)
    print(f"  {len(groups):,} album/artist groups\n")

    tags_by_album = find_tags(groups, skip_numbered=not include_numbered)

    ignores_path = os.path.abspath(ignores_file) if use_ignores else None
    ignores: set[tuple[str, str]] = load_ignores(ignores_path) if ignores_path else set()
    if ignores:
        before = len(tags_by_album)
        tags_by_album = {k: v for k, v in tags_by_album.items() if k not in ignores}
        skipped = before - len(tags_by_album)
        if skipped:
            print(f"  {C['DIM']}{skipped} album(s) suppressed by ignore list{C['RESET']}")

    total_tracks = sum(len(v) for v in tags_by_album.values())
    if total_tracks == 0:
        print("No tracks need tagging. All done.")
        return

    print(
        f"Found {C['BOLD']}{total_tracks}{C['RESET']} track(s) to tag "
        f"across {C['BOLD']}{len(tags_by_album)}{C['RESET']} album(s).\n"
        f"You will be shown each album's proposed tags and asked to approve them.\n"
        f"  {C['GREEN']}Green{C['RESET']}   = Work / Grouping / Movement Name\n"
        f"  {C['MAGENTA']}Magenta{C['RESET']} = Movement Number / Count"
    )

    approved, newly_ignored = approval_loop(
        tags_by_album,
        tracks_by_album=groups,
        skip_numbered=not include_numbered,
    )

    if newly_ignored and ignores_path:
        ignores.update(newly_ignored)
        save_ignores(ignores_path, ignores)
        print(
            f"  {C['DIM']}Added {len(newly_ignored)} album(s) to ignore list "
            f"({ignores_path}){C['RESET']}"
        )

    if not approved:
        print("No changes applied.")
        return

    # approved tuples: (pid, work, mvt_name, mvt_num, mvt_cnt, file_path)
    as_ops = [t[:5] for t in approved]  # AppleScript only needs first 5 fields

    print(f"\nApplying tags to {len(approved)} track(s) in Apple Music…")
    print(f"  (Make sure Apple Music is running)\n")

    as_success = 0
    for i in range(0, len(as_ops), CHUNK):
        chunk = as_ops[i : i + CHUNK]
        end   = min(i + CHUNK, len(as_ops))
        print(f"  Tagging tracks {i + 1}–{end}…", end='', flush=True)
        if tag_via_applescript(chunk):
            as_success += len(chunk)
            print(f" {C['GREEN']}✓{C['RESET']}")
        else:
            print(f" {C['RED']}✗  (see error above){C['RESET']}")

    print(
        f"\n{C['BOLD']}Apple Music tags:{C['RESET']} "
        f"{as_success}/{len(approved)} tracks tagged successfully."
    )
    if as_success < len(approved):
        print(
            f"{C['YELLOW']}Note:{C['RESET']} Some AppleScript operations failed. "
            "Re-export Library.xml from Apple Music and try again if the "
            "persistent IDs no longer match the live library."
        )

    # --- Write shwm atom to MP4 files ----------------------------------------
    mp4_targets = [
        (pid, path)
        for pid, _work, _mvt, _num, _cnt, path in approved
        if path and path.lower().endswith(('.m4a', '.mp4', '.m4p'))
    ]

    if not mp4_targets:
        print("\nNo MP4/M4A files to update with shwm atom.")
    else:
        print(f"\nWriting shwm atom to {len(mp4_targets)} MP4/M4A file(s)…\n")
        shwm_ok   = 0
        shwm_fail = 0
        for pid, path in mp4_targets:
            try:
                write_shwm_to_mp4(path)
                print(f"  {C['GREEN']}✓{C['RESET']}  {os.path.basename(path)}")
                shwm_ok += 1
            except Exception as exc:
                print(
                    f"  {C['RED']}✗{C['RESET']}  {os.path.basename(path)}: {exc}",
                    file=sys.stderr,
                )
                shwm_fail += 1

        print(
            f"\n{C['BOLD']}shwm atoms:{C['RESET']} "
            f"{shwm_ok} written, {shwm_fail} failed."
        )
        if shwm_fail:
            sys.exit(1)


if __name__ == '__main__':
    main()
