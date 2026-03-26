#!/usr/bin/env python3
"""
Apple Music Classical Tagger
-----------------------------
Sets Work, Movement Name, Movement Number, and Movement Count tags on
classical music tracks derived from their existing titles.

A title must contain a colon to be processed:
  "Symphony No. 5 in C minor, Op. 67: II. Andante con moto"
   → Work:            "Symphony No. 5 in C minor, Op. 67"
   → Movement Name:   "Andante con moto"
   → Movement Number: 2
   → Movement Count:  <highest Roman numeral found across same-work tracks>

Within each album, all tracks sharing the same work prefix (the text before
the first colon) are considered part of the same work.  The movement count is
the highest Arabic value obtained by converting the leading Roman numeral of
any track in that group.

Usage:
    python3 tag_classical.py              # Library.xml in cwd or ~/Music/Music/Library.xml
    python3 tag_classical.py /path/to/Library.xml

Requires: Python 3.6+, standard library only.
Tags are applied via AppleScript (macOS only).  Apple Music must be running.
"""

import sys
import os
import re
import plistlib
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass

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


def find_tags(tracks_by_album: dict) -> dict:
    """
    Walk each album group in disc/track order and propose Work / Movement Name /
    Movement Number / Movement Count tags derived from the track titles.

    Only tracks whose title contains a colon are considered.  Within each album
    the work prefix (everything before the first colon) is used to group tracks
    into a single piece; the movement count is the highest Roman numeral value
    seen across all tracks in that group.

    Tracks whose existing tags already match the proposed values are skipped.

    Returns:
        dict mapping (album, artist) -> list[TagChange]
    """
    result = {}

    for album_key, tracks in tracks_by_album.items():
        ordered = sorted(
            tracks,
            key=lambda t: (t.get('Disc Number', 1), t.get('Track Number', 0)),
        )

        # --- Pass 1: parse each track title ----------------------------------
        # Tuple layout: (track, title, work, movement_name, roman_str, arabic_int)
        parsed: list[tuple] = []
        for track in ordered:
            title = track.get('Name', '').strip()
            if ':' not in title:
                continue
            work, _, rest = title.partition(':')
            work          = work.strip()
            movement_name = rest.strip()
            roman_str, arabic_int = extract_leading_roman(movement_name)
            if roman_str is not None:
                tail = movement_name[len(roman_str):]
                movement_name = tail.lstrip('. ').strip()
            parsed.append((track, title, work, movement_name, roman_str, arabic_int))

        if not parsed:
            continue

        # --- Pass 2: compute movement count per work -------------------------
        # movement_count = highest arabic movement number seen in any track
        # that shares the same work prefix within this album.
        work_max: dict[str, int] = defaultdict(int)
        for _track, _title, work, _mvt_name, _roman, arabic_int in parsed:
            if arabic_int is not None:
                work_max[work] = max(work_max[work], arabic_int)

        # --- Pass 3: build TagChange list, skipping already-correct tracks ---
        album_changes: list[TagChange] = []
        for track, title, work, movement_name, _roman_str, arabic_int in parsed:
            movement_number = arabic_int
            movement_count  = work_max.get(work) if arabic_int is not None else None

            # Skip if every proposed tag already matches the library value.
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

        if album_changes:
            result[album_key] = album_changes

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

def prompt(msg: str) -> str:
    """Read a single-character response."""
    try:
        raw = input(msg).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 'q'
    return raw[:1] if raw else ''


def approval_loop(tags_by_album: dict) -> list[tuple]:
    """
    Present each album's proposed tag changes to the user and collect approvals.

    Returns a flat list of
        (persistent_id, work, movement_name, movement_number, movement_count)
    ready to pass to tag_via_applescript.
    """
    approved: list[tuple] = []
    auto_approve = False

    albums = sorted(
        tags_by_album.items(),
        key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()),
    )
    total = len(albums)

    for idx, (album_key, changes) in enumerate(albums, 1):
        display_album(album_key, changes)

        if auto_approve:
            _collect(approved, changes)
            continue

        n = len(changes)
        print(f"  Album {idx}/{total}  —  {n} track(s) to tag")

        while True:
            resp = prompt("  Apply? [y]es / [n]o / [s]elect / [a]ll remaining / [q]uit  > ")
            if resp == 'y':
                _collect(approved, changes)
                break
            elif resp == 'n':
                print(f"  {C['DIM']}Skipped.{C['RESET']}")
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
            elif resp == 'a':
                auto_approve = True
                _collect(approved, changes)
                break
            elif resp == 'q':
                print(
                    f"\n{C['YELLOW']}Quit — {len(approved)} tag operation(s) "
                    f"already approved will be applied.{C['RESET']}"
                )
                return approved
            else:
                print("  Please enter  y / n / s / a / q")

    return approved


def _collect(approved: list, changes: list[TagChange]) -> None:
    """Append approved tag ops from *changes* to *approved*."""
    for change in changes:
        pid = change.track.get('Persistent ID')
        if not pid:
            continue
        approved.append((
            pid,
            change.work,
            change.movement_name,
            _as_int(change.movement_number),
            _as_int(change.movement_count),
        ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CHUNK = 50  # tag operations per osascript call


def main():
    # --- Locate library XML --------------------------------------------------
    if len(sys.argv) > 1:
        library_path = os.path.expanduser(sys.argv[1])
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

    print(f"Loading library: {library_path}")
    library      = load_library(library_path)
    tracks_dict  = library.get('Tracks', {})
    print(f"  {len(tracks_dict):,} tracks found")

    groups = group_tracks_by_album(tracks_dict)
    print(f"  {len(groups):,} album/artist groups\n")

    tags_by_album = find_tags(groups)

    total_tracks = sum(len(v) for v in tags_by_album.values())
    if total_tracks == 0:
        print("No tracks need tagging. All done.")
        return

    print(
        f"Found {C['BOLD']}{total_tracks}{C['RESET']} track(s) to tag "
        f"across {C['BOLD']}{len(tags_by_album)}{C['RESET']} album(s).\n"
        f"You will be shown each album's proposed tags and asked to approve them.\n"
        f"  {C['GREEN']}Green{C['RESET']}   = Work / Movement Name\n"
        f"  {C['MAGENTA']}Magenta{C['RESET']} = Movement Number / Count"
    )

    approved = approval_loop(tags_by_album)

    if not approved:
        print("No changes applied.")
        return

    print(f"\nApplying tags to {len(approved)} track(s) in Apple Music…")
    print(f"  (Make sure Apple Music is running)\n")

    success = 0
    for i in range(0, len(approved), CHUNK):
        chunk = approved[i : i + CHUNK]
        end   = min(i + CHUNK, len(approved))
        print(f"  Tagging tracks {i + 1}–{end}…", end='', flush=True)
        if tag_via_applescript(chunk):
            success += len(chunk)
            print(f" {C['GREEN']}✓{C['RESET']}")
        else:
            print(f" {C['RED']}✗  (see error above){C['RESET']}")

    print(f"\n{C['BOLD']}Done.{C['RESET']} {success}/{len(approved)} tracks tagged successfully.")
    if success < len(approved):
        print(
            f"{C['YELLOW']}Note:{C['RESET']} Some operations failed. "
            "Re-export Library.xml from Apple Music and try again if the "
            "persistent IDs no longer match the live library."
        )


if __name__ == '__main__':
    main()
