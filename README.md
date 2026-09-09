# TagSplice

Simple script to split `ARTIST` and `DISPLAYARTIST` tag for your music.


## Why bother?
'Cause it looks nicer, DUH~!
would you prefer `Artist A; Artist B; Artist C`
or  `Artist A & Artist B feat. Artist C` (`Artist A//Artist B//ArtistC`) ?

If you are the kind of person that prefers this and not quite satisfied with something like Picard or Mp3tag editor, then this might be for you.
## Features
- Auto-extracts relevent tags and splice them.
- If the track title includes `feat. / ft.` embed into tags (entirely optionall)
- Intuitive and easy to use Qt GUI available (if not present, tk fallback available)
- Allows users to add custome overrides so you can have the notation you prefer.

## Usage

Requires Python 3.10+ and Mutagen. The GUI also requires PySide6 and tkinterdnd2.

```
python run.py cli view path\to\file.flac (or entire directory)
python run.py cli fix --apply path\to\folder
```

`run.py` creates `.venv` and installs dependencies on first use. The CLI previews changes by default. Use `--apply` to write them. Use `--extract-feat-from-title` to enable optional title-credit extraction.


## GUI
Requires PySide6 (fallback uses tk if Qt is not available)

`python run.py gui` to run GUI. Qt should be auto-installed.

## Overrides

Utilize `overrides.json` to use your preferred notation especially in `DISPLAYARTIST` field.
Schema: `"original_notation": "preferred_notation"`

```json
{
  "artist a": "Artist A",
  "Band B": "band B",
  "chorus C": "CHORUS c"
}
```

