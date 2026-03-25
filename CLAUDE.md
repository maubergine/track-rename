# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the script

```bash
python3 rename_movements.py              # uses Library.xml in cwd or ~/Music/Music/Library.xml
python3 rename_movements.py /path/to/Library.xml
```

The script requires macOS with Apple Music running when it reaches the rename step. Library.xml is exported from Apple Music via File › Library › Export Library…

## Architecture

Everything lives in `rename_movements.py`. The pipeline is:

1. **Parse** — `load_library` reads the XML plist; `group_tracks_by_album` buckets tracks by `(album, artist)`.
2. **Detect** — a rename strategy walks each album's tracks in disc/track order, maintaining a rolling `current_prefix`. Two strategies exist:
   - `find_renames` (default): sets `current_prefix` from any anchor track (`Piece: I Movement`) and prefixes orphaned movements (those starting with Roman numeral II+).
   - `find_renames_parts_sections` (alternate): also handles section-divider tracks like `Seconda Parte: I …` or `Post Copulationem: I …`, keeping a separate `current_piece_root` so the full `Piece: Section: Movement` hierarchy is preserved.
3. **Approve** — `approval_loop` presents each album interactively. The `[t]ry alternate strategy` option re-runs the album through the next strategy in `ALTERNATE_STRATEGIES` without re-parsing the library.
4. **Apply** — `rename_via_applescript` batches approved renames into osascript calls (50 at a time).

## Key patterns

**Anchor track**: `"Piece Name: I First Movement"` — sets the running prefix; never renamed.

**Orphan**: starts with Roman numeral II or higher with no piece prefix — gets the current prefix prepended.

**Section divider** (alternate strategy only): `"Seconda Parte: I …"` or `"Post Copulationem: I …"` — the prefix before the colon matches `_SECTION_NAME`; the track itself is renamed to include the piece root.

**Anchor with embedded section**: `"Piece: Prima Parte: I …"` — matched by `_ANCHOR_WITH_SECTION`; splits into `current_piece_root` and `current_prefix`.

## Adding a new alternate strategy

Register it in `ALTERNATE_STRATEGIES` at the bottom of the rename-detection section:

```python
ALTERNATE_STRATEGIES: dict[str, tuple[str, object]] = {
    'parts': ('Parts/sections (…)', find_renames_parts_sections),
    'my_strategy': ('Description shown to user', my_strategy_fn),
}
```

The callable must have the signature `(tracks_by_album: dict) -> dict` matching `find_renames`.
