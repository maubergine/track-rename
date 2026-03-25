#!/usr/bin/env python3
"""
Apple Music Track Renamer
-------------------------
Fixes classical music movement naming by prefixing orphaned movement titles
(e.g. "II Allemande") with their parent piece name
(e.g. "Partita No. 1, in B flat major, BWV 825: II Allemande").

Usage:
    python3 rename_movements.py [Library.xml]

If no path is given the script looks for Library.xml in the current directory,
then falls back to ~/Music/Music/Library.xml.

Requires: Python 3.6+, standard library only.
Renames are applied via AppleScript (macOS only).  Apple Music must be running.
"""

import sys
import os
import re
import plistlib
import subprocess
import tempfile
from collections import defaultdict

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
# Roman-numeral helpers
# ---------------------------------------------------------------------------

# All Roman numerals that can plausibly appear as movement numbers (I–C, i.e. 1–100).
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

_ROMAN = {_roman(n) for n in range(1, 101)}

# Pattern: starts with a Roman numeral then a space (e.g. "II Allemande").
# Characters cover I–C (1–100): I V X L C.
_STARTS_ROMAN = re.compile(r'^([IVXLC]+)\s')

# Pattern: "Piece Name: ROMAN_NUMERAL Movement Name"
# Uses a lazy match so it stops at the first ": ROMAN space".
_ANCHOR = re.compile(r'^(.*?):\s+([IVXLC]+)\s')

# ---------------------------------------------------------------------------
# Section-divider patterns (used by the parts/sections alternate strategy)
# ---------------------------------------------------------------------------
# Matches strings that are structural section headings, not piece names:
#   "Prima/Seconda/… Parte", "Post …", "Ante …", "Pars I/II/…"
_SECTION_PATTERN_STR = (
    r'(?:Prima|Seconda|Terza|Quarta|Quinta|Sesta)\s+Parte'
    r'|Post\s+\w+(?:\s+\w+)*'
    r'|Ante\s+\w+(?:\s+\w+)*'
    r'|Pars\s+[IVXLC]+'
)
# Matches a *complete* section name string (used to classify a parsed prefix).
_SECTION_NAME = re.compile(
    r'^(?:' + _SECTION_PATTERN_STR + r')$',
    re.IGNORECASE,
)
# Matches an anchor track that already embeds both piece name and section name:
#   "Piece Name: Prima Parte: I Movement"
# Groups: (1) piece root, (2) section name, (3) Roman numeral
_ANCHOR_WITH_SECTION = re.compile(
    r'^(.*?)\s*:\s*(' + _SECTION_PATTERN_STR + r')\s*:\s*([IVXLC]+)\s',
    re.IGNORECASE,
)


def _leading_roman(name: str):
    """Return the leading Roman numeral token (upper-cased), or None."""
    m = _STARTS_ROMAN.match(name)
    if not m:
        return None
    token = m.group(1).upper()
    return token if token in _ROMAN else None


def parse_anchor(name: str):
    """
    If *name* has the anchor format "Piece: I Movement", return (piece_prefix, numeral).
    Otherwise return (None, None).
    """
    m = _ANCHOR.match(name)
    if not m:
        return None, None
    numeral = m.group(2).upper()
    if numeral not in _ROMAN:
        return None, None
    return m.group(1), numeral


def is_orphan(name: str) -> bool:
    """
    True if *name* appears to be an orphaned movement title – i.e. it starts
    with a Roman numeral II or higher with no preceding piece name.
    """
    token = _leading_roman(name)
    return token is not None and token != 'I'


# ---------------------------------------------------------------------------
# Library parsing
# ---------------------------------------------------------------------------

def load_library(path: str) -> dict:
    with open(path, 'rb') as fh:
        return plistlib.load(fh)


def group_tracks_by_album(tracks_dict: dict) -> dict:
    """
    Group all *File* / *Remote* tracks by (album, artist).
    Returns dict mapping that key to a list of track dicts.
    """
    groups: dict = defaultdict(list)
    for _tid, track in tracks_dict.items():
        if track.get('Track Type', '') not in ('File', 'Remote'):
            continue
        album  = track.get('Album', '')
        artist = track.get('Artist') or track.get('Album Artist', '')
        groups[(album, artist)].append(track)
    return groups


# ---------------------------------------------------------------------------
# Rename detection
# ---------------------------------------------------------------------------

def find_renames(tracks_by_album: dict) -> dict:
    """
    Walk each album group in disc/track order, maintain a rolling
    *current_piece_prefix* (updated whenever an anchor track is encountered),
    and collect orphaned movements that need prefixing.

    Returns:
        dict mapping (album, artist) -> list of (track_dict, old_name, new_name)
    """
    result = {}

    for album_key, tracks in tracks_by_album.items():
        # Sort by disc then track number
        ordered = sorted(
            tracks,
            key=lambda t: (t.get('Disc Number', 1), t.get('Track Number', 0))
        )

        current_prefix: str | None = None
        album_renames = []

        for track in ordered:
            name = track.get('Name', '').strip()
            if not name:
                continue

            piece_prefix, numeral = parse_anchor(name)
            if piece_prefix is not None:
                # Anchor track: update running piece prefix, no rename needed.
                current_prefix = piece_prefix
                continue

            if is_orphan(name) and current_prefix is not None:
                new_name = f"{current_prefix}: {name}"
                album_renames.append((track, name, new_name))
            # Non-anchor, non-orphan tracks leave current_prefix unchanged.

        if album_renames:
            result[album_key] = album_renames

    return result


def find_renames_parts_sections(tracks_by_album: dict) -> dict:
    """
    Alternate strategy: handle structural section dividers such as
    "Prima Parte", "Seconda Parte", and "Post Copulationem".

    Under the default strategy a track like "Seconda Parte: I Recitativo…"
    is parsed as a piece anchor with prefix "Seconda Parte", causing
    subsequent movements to lose the real piece name.  This strategy instead
    recognises such prefixes as section updates, renames the section-boundary
    track to include the piece root, and then prefixes subsequent orphans with
    "Piece Root: Section Name".
    """
    result = {}

    for album_key, tracks in tracks_by_album.items():
        ordered = sorted(
            tracks,
            key=lambda t: (t.get('Disc Number', 1), t.get('Track Number', 0)),
        )

        current_prefix: str | None = None
        current_piece_root: str | None = None
        album_renames = []

        for track in ordered:
            name = track.get('Name', '').strip()
            if not name:
                continue

            # --- Case 1: anchor track with embedded section ----------------
            # e.g. "Wer Dank opfert, BWV 17: Prima Parte: I [Coro]: …"
            m = _ANCHOR_WITH_SECTION.match(name)
            if m:
                piece_root = m.group(1).rstrip()
                section    = m.group(2)
                current_piece_root = piece_root
                current_prefix     = f"{piece_root}: {section}"
                # This track's name is already correct; no rename needed.
                continue

            # --- Case 2: plain anchor or section-boundary track ------------
            piece_prefix, numeral = parse_anchor(name)
            if piece_prefix is not None:
                if _SECTION_NAME.match(piece_prefix) and current_piece_root is not None:
                    # e.g. "Seconda Parte: I Recitativo…"
                    # Treat as a section update: rename to include piece root.
                    new_name = f"{current_piece_root}: {name}"
                    album_renames.append((track, name, new_name))
                    current_prefix = f"{current_piece_root}: {piece_prefix}"
                else:
                    # Normal piece anchor (no section recognised).
                    current_piece_root = piece_prefix
                    current_prefix     = piece_prefix
                continue

            # --- Case 3: orphaned movement ---------------------------------
            if is_orphan(name) and current_prefix is not None:
                new_name = f"{current_prefix}: {name}"
                album_renames.append((track, name, new_name))

        if album_renames:
            result[album_key] = album_renames

    return result


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------
# Maps a short key to (human-readable description, callable).
# The callable has the same signature as find_renames(tracks_by_album) -> dict.
# Add further strategies here as needed.
ALTERNATE_STRATEGIES: dict[str, tuple[str, object]] = {
    'parts': (
        'Parts/sections  '
        '(handles Prima Parte / Seconda Parte / Post Copulationem dividers)',
        find_renames_parts_sections,
    ),
}


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _highlight_diff(old_name: str, new_name: str) -> str:
    """Return the new name with the added prefix highlighted in green."""
    if new_name.endswith(old_name):
        added = new_name[: len(new_name) - len(old_name)]
        return f"{C['GREEN']}{added}{C['RESET']}{old_name}"
    return new_name


def display_album(album_key, changes, *, show_disc: bool = False, strategy_label: str = ''):
    album, artist = album_key
    disc_nums = sorted({t.get('Disc Number', 1) for t, _, _ in changes})

    print(f"\n{C['BOLD']}Album:{C['RESET']}  {album}")
    print(f"{C['BOLD']}Artist:{C['RESET']} {artist}")
    if show_disc and len(disc_nums) > 1:
        print(f"{C['BOLD']}Discs:{C['RESET']}  {', '.join(map(str, disc_nums))}")
    if strategy_label:
        print(f"{C['YELLOW']}Strategy:{C['RESET']} {strategy_label}")
    print(f"\n  {C['DIM']}{'Tr':>3}  {'Disc':>4}  Before / After{C['RESET']}")
    print(f"  {'─' * 72}")

    for track, old_name, new_name in changes:
        tr  = track.get('Track Number', '?')
        dis = track.get('Disc Number', 1)
        highlighted = _highlight_diff(old_name, new_name)
        label_w = 3
        print(f"  {C['CYAN']}{str(tr):>{label_w}}{C['RESET']}  {C['DIM']}{str(dis):>4}{C['RESET']}"
              f"  {C['DIM']}Before:{C['RESET']} {old_name}")
        print(f"  {' ' * label_w}  {' ' * 4}   {C['DIM']} After:{C['RESET']} {highlighted}")

    print()


# ---------------------------------------------------------------------------
# AppleScript renaming
# ---------------------------------------------------------------------------

def _as_str(s: str) -> str:
    """Escape a Python string for use inside an AppleScript double-quoted string."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def rename_via_applescript(renames: list[tuple[str, str]]) -> bool:
    """
    Apply *renames* (list of (persistent_id, new_name)) in Apple Music
    using a single osascript call.
    """
    if not renames:
        return True

    entries = ', '.join(
        f'{{"{_as_str(pid)}", "{_as_str(name)}"}}'
        for pid, name in renames
    )

    script = f'''\
tell application "Music"
    set renameList to {{{entries}}}
    repeat with entry in renameList
        set theID to item 1 of entry
        set theNewName to item 2 of entry
        try
            set theTrack to first file track of library playlist 1 whose persistent ID is theID
            set name of theTrack to theNewName
        on error errMsg
            log "Could not rename " & theID & ": " & errMsg
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
            capture_output=True, text=True, timeout=600
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
    """Read a single-character response, stripping whitespace."""
    try:
        raw = input(msg).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 'q'
    return raw[:1] if raw else ''


def approval_loop(renames_by_album: dict, tracks_by_album: dict) -> list[tuple[str, str]]:
    """
    Present each album's proposed renames to the user.

    Extra option [t]ry alternate strategy regenerates renames for the current
    album using the next registered strategy and re-displays the results.
    Pressing [t] again cycles to the next strategy (or back to the default).

    Returns the flat list of (persistent_id, new_name) pairs to apply.
    """
    approved: list[tuple[str, str]] = []
    auto_approve = False

    # Build the ordered strategy list: default (index 0) then alternates.
    strategies: list[tuple[str, object | None]] = [('Default', None)]
    for _key, (desc, fn) in ALTERNATE_STRATEGIES.items():
        strategies.append((desc, fn))
    has_alternates = len(strategies) > 1

    albums = sorted(renames_by_album.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()))
    total_albums = len(albums)

    for idx, (album_key, default_changes) in enumerate(albums, 1):
        strategy_idx = 0
        current_changes = default_changes

        while True:  # outer loop: re-enter after a strategy switch
            strategy_label, _ = strategies[strategy_idx]
            label_for_display = '' if strategy_idx == 0 else strategy_label

            if current_changes:
                multi_disc = len({t.get('Disc Number', 1) for t, _, _ in current_changes}) > 1
                display_album(album_key, current_changes, show_disc=multi_disc,
                              strategy_label=label_for_display)
            else:
                album, artist = album_key
                print(f"\n{C['BOLD']}Album:{C['RESET']}  {album}")
                print(f"{C['BOLD']}Artist:{C['RESET']} {artist}")
                print(f"{C['YELLOW']}Strategy:{C['RESET']} {strategy_label}")
                print(f"\n  {C['YELLOW']}No renames found with this strategy.{C['RESET']}")

            if auto_approve:
                approved.extend(
                    (t['Persistent ID'], new_name)
                    for t, _, new_name in current_changes
                    if t.get('Persistent ID')
                )
                break

            n = len(current_changes)
            print(f"\n  Album {idx}/{total_albums}  —  {n} rename(s) proposed")

            prompt_opts = "[y]es / [n]o / [a]ll remaining / [q]uit"
            if has_alternates:
                prompt_opts += " / [t]ry alternate strategy"

            decided = False
            while not decided:
                resp = prompt(f"  Apply? {prompt_opts}  > ")
                if resp == 'y':
                    approved.extend(
                        (t['Persistent ID'], new_name)
                        for t, _, new_name in current_changes
                        if t.get('Persistent ID')
                    )
                    decided = True
                elif resp == 'n':
                    print(f"  {C['DIM']}Skipped.{C['RESET']}")
                    decided = True
                elif resp == 'a':
                    auto_approve = True
                    approved.extend(
                        (t['Persistent ID'], new_name)
                        for t, _, new_name in current_changes
                        if t.get('Persistent ID')
                    )
                    decided = True
                elif resp == 'q':
                    print(
                        f"\n{C['YELLOW']}Quit – {len(approved)} rename(s) "
                        f"already approved will be applied.{C['RESET']}"
                    )
                    return approved
                elif resp == 't' and has_alternates:
                    # Cycle to next strategy (wraps back to default).
                    strategy_idx = (strategy_idx + 1) % len(strategies)
                    _, fn = strategies[strategy_idx]
                    if fn is None:
                        current_changes = default_changes
                    else:
                        alt = fn({album_key: tracks_by_album[album_key]})
                        current_changes = alt.get(album_key, [])
                    break  # break inner loop → re-display with new strategy
                else:
                    valid = 'y / n / a / q' + (' / t' if has_alternates else '')
                    print(f"  Please enter  {valid}")

            if decided:
                break  # done with this album

    return approved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CHUNK = 50  # renames per osascript call


def main():
    # --- Locate the library XML ---
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
                "Pass the path explicitly:  python3 rename_movements.py /path/to/Library.xml\n\n"
                "To export from Apple Music: File › Library › Export Library…",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Loading library: {library_path}")
    library = load_library(library_path)

    tracks_dict = library.get('Tracks', {})
    print(f"  {len(tracks_dict):,} tracks found")

    groups = group_tracks_by_album(tracks_dict)
    print(f"  {len(groups):,} album/artist groups\n")

    renames_by_album = find_renames(groups)

    total_renames = sum(len(v) for v in renames_by_album.values())
    if total_renames == 0:
        print("No tracks need renaming. All done.")
        return

    print(
        f"Found {C['BOLD']}{total_renames}{C['RESET']} track(s) to rename "
        f"across {C['BOLD']}{len(renames_by_album)}{C['RESET']} album(s).\n"
        f"You will be shown each album's changes and asked to approve them.\n"
        f"  {C['GREEN']}Green text{C['RESET']} = text being added to the title."
    )

    approved = approval_loop(renames_by_album, groups)

    if not approved:
        print("No changes applied.")
        return

    print(f"\nApplying {len(approved)} rename(s) to Apple Music…")
    print(f"  (Make sure Apple Music is running)\n")

    success = 0
    for i in range(0, len(approved), CHUNK):
        chunk = approved[i : i + CHUNK]
        end   = min(i + CHUNK, len(approved))
        print(f"  Renaming tracks {i+1}–{end}…", end='', flush=True)
        if rename_via_applescript(chunk):
            success += len(chunk)
            print(f" {C['GREEN']}✓{C['RESET']}")
        else:
            print(f" {C['RED']}✗  (see error above){C['RESET']}")

    print(f"\n{C['BOLD']}Done.{C['RESET']} {success}/{len(approved)} tracks renamed successfully.")
    if success < len(approved):
        print(
            f"{C['YELLOW']}Note:{C['RESET']} Some renames failed. "
            "This can happen if the persistent IDs in the XML no longer match "
            "the live library (re-export Library.xml and try again)."
        )


if __name__ == '__main__':
    main()
