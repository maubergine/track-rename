# track-rename

Fixes classical music movement naming in Apple Music by prefixing orphaned movement titles with their parent piece name.

**Before:**
```
II Allemande
III Corrente
IV Sarabande
```

**After:**
```
Partita No. 1, in B flat major, BWV 825: II Allemande
Partita No. 1, in B flat major, BWV 825: III Corrente
Partita No. 1, in B flat major, BWV 825: IV Sarabande
```

## Requirements

- macOS with Apple Music
- Python 3.6+

## Usage

1. Export your library from Apple Music: **File › Library › Export Library…**
2. Run the script:

```bash
python3 rename_movements.py                        # looks for Library.xml in cwd
python3 rename_movements.py /path/to/Library.xml
```

3. For each album with proposed renames, review the before/after diff and choose:
   - `y` — apply renames for this album
   - `n` — skip this album
   - `a` — apply this and all remaining albums without further prompting
   - `q` — quit (renames already approved are still applied)
   - `t` — try an alternate naming strategy for this album (see below)

## Alternate strategy: Parts/Sections

Some works (particularly Bach cantatas) are divided into named sections — *Prima Parte*, *Seconda Parte*, *Post Copulationem*, etc. — where the section name appears as a prefix before the movement number. The default strategy misidentifies these as piece names.

Pressing `t` at the album prompt switches to the **Parts/Sections** strategy, which recognises these section dividers and produces names like:

```
Wer Dank opfert, der preiset mich, BWV 17: Seconda Parte: I Recitativo (Tenore)
```

Pressing `t` again cycles back to the default strategy.
