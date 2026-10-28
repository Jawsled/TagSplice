# TagSplice

Simple script to split `ARTIST` and `DISPLAYARTIST` tag for your music.


## Why bother?
'Cause it looks nicer, DUH~!
would you prefer `Artist A; Artist B; Artist C`
or  `Artist A & Artist B feat. Artist C` (`Artist A//Artist B//ArtistC`) ?

If you are the kind of person that prefers this and not quite satisfied with something like Picard or Mp3tag editor, then this might be for you.
## Features
- Auto-extracts relevent tags and splice them.
- If the track title includes `feat. / ft.` or a remix credit, embed the credit into tags (optional)
- Intuitive and easy to use Qt GUI available (if not present, tk fallback available)
- Allows users to add custom overrides so you can have the notation you prefer, editable from the GUI.

## Usage

Requires Python 3.10+ and Mutagen. The GUI also requires PySide6 and tkinterdnd2.

```
python run.py cli view path\to\file.flac (or entire directory)
python run.py cli fix --apply path\to\folder
```

`run.py` creates `.venv` and installs dependencies on first use. The CLI previews changes by default. Use `--apply` to write them. Use `--extract-feat-from-title` (alias `--extract-title-credits`) to enable optional title-credit extraction: `(feat. X)` moves into `ARTIST`/`DISPLAYARTIST`, while `(DJ X Remix)`/`[DJ X Remix]` fills `REMIXER`/`TPE4` without changing the title or display artist.


## GUI
Requires PySide6 (fallback uses tk if Qt is not available)

`python run.py gui` to run GUI. Qt should be auto-installed.

## Overrides

Utilize `overrides.json` to use your preferred notation especially in `DISPLAYARTIST` field.
Schema: `"original_notation": "preferred_notation"` (matching ignores case).

```json
{
  "artist a": "Artist A",
  "Band B": "band B",
  "chorus C": "CHORUS c"
}
```

In the GUI, click **Overrides…** to add, edit, or remove rules. It writes the
same plain-JSON `overrides.json`, so the CLI keeps using it too, and hand edits
are picked up on the next preview.

