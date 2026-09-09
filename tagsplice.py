#!/usr/bin/env python3
"""
TagSplice - view and normalize ID3/Vorbis tags for audio files.

Normalizations:
  * ARTIST: split "Primary feat. X & Y" into multiple ARTIST values,
    one per artist, with title-cased names (overrides + str.title() fallback).
  * ALBUMARTIST: when multiple artists are listed, join them with " ; "
    (the standard multi-value separator for album-artist groupings).
  * TITLE feat. extraction (OPTIONAL, off by default): move
    "(feat. X)" / "(ft. X)" / "[feat. X]" suffixes from TITLE into
    ARTIST (as extra values) and DISPLAY ARTIST ("Primary feat. X"),
    and strip the suffix from TITLE. Enable with
    `--extract-feat-from-title` (CLI) or the GUI checkbox.

Usage:
  python tagsplice.py view <path> [<path> ...]
  python tagsplice.py fix [--apply] [--albumartist-sep " ; "]
    [--extract-feat-from-title] <path> [<path> ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.oggvorbis import OggVorbis

OVERRIDES_PATH = Path(__file__).with_name("overrides.json")

FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
AMP_RE = re.compile(r"\s*&\s*")
COMMA_RE = re.compile(r"\s*,\s*")
# EDIT_SEP must be defined before split_artists uses it.
EDIT_SEP = "//"
EDIT_SEP_RE = re.compile(r"\s*//\s*")
SEMICOLON_RE = re.compile(r"\s*;\s*")

# Parenthesized/bracketed feat. credit inside TITLE, e.g.
#   "SOS (feat. Aloe Blacc)", "Song [ft. Rita Ora]".
# Only the parenthesized form is matched so plain words like
# "Featuring Henry" in a title are never touched.
TITLE_FEAT_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]",
    re.IGNORECASE,
)


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k.lower(): v for k, v in data.items()}


def split_artists(artist_field: str) -> list[str]:
    """Split 'Primary feat. X & Y' into ['Primary', 'X', 'Y'].

    Handles all common in-band separators automatically:
      '//' (TagSplice multi-value marker), ';', 'feat./ft./featuring',
      '&', and ','. Surrounding whitespace is trimmed and empty
      segments are dropped.
    """
    # Split feat. off first so 'A feat. B & C' -> ['A', 'B & C'].
    parts = FEAT_RE.split(artist_field, maxsplit=1)
    if len(parts) == 2:
        primary, featured = parts
        chunks = [primary, featured]
    else:
        chunks = [parts[0]]
    # Then split every chunk on //, ;, & and comma.
    out: list[str] = []
    for chunk in chunks:
        for piece in EDIT_SEP_RE.split(chunk):
            for piece in SEMICOLON_RE.split(piece):
                for piece in AMP_RE.split(piece):
                    for piece in COMMA_RE.split(piece):
                        piece = piece.strip()
                        if piece:
                            out.append(piece)
    return out or [artist_field]


# Separator users type into the editable After cell.
# (EDIT_SEP is defined above so split_artists can use it.)


def extract_title_feat(title: str) -> tuple[str, list[str]]:
    """Split "(feat. X)" / "(ft. X)" out of a TITLE.

    Returns (cleaned_title, featured_names). featured_names are raw
    (unformatted) names, already split on '&', ',', ';', '//'.
    If no parenthesized feat. credit is present, returns the title
    unchanged and an empty list.
    """
    featured: list[str] = []
    for match in TITLE_FEAT_RE.finditer(title):
        inner = match.group(1).strip()
        if not inner:
            continue
        # Re-use split_artists for '&'/',' handling; the inner text
        # contains no feat. keyword so only those separators apply.
        for name in split_artists(inner):
            name = name.strip()
            if name:
                featured.append(name)
    if not featured:
        return title, []
    cleaned = TITLE_FEAT_RE.sub("", title)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # Drop a dangling " -" left behind, e.g. "Song - (feat. X)" -> "Song".
    cleaned = re.sub(r"\s*-\s*$", "", cleaned).strip()
    return cleaned, featured


def parse_edit_value(text: str) -> list[str]:
    """Parse user-typed text back into a list of tag values.

    Splits on EDIT_SEP and trims whitespace; drops empty segments.
    """
    return [v.strip() for v in text.split(EDIT_SEP) if v.strip()]


def format_artist(name: str, overrides: dict[str, str]) -> str:
    key = name.strip().lower()
    if key in overrides:
        return overrides[key]
    if not key:
        return name
    return name.strip().title()


def open_audio(path: Path):
    suf = path.suffix.lower()
    if suf == ".flac":
        return FLAC(str(path))
    if suf == ".mp3":
        return MP3(str(path))
    if suf in (".ogg", ".oga"):
        return OggVorbis(str(path))
    return None


def read_tags(audio) -> dict[str, list[str]]:
    """Return tag -> [values] for the keys we care about.

    Vorbis tags are case-insensitive, so we normalize keys to uppercase.
    """
    out: dict[str, list[str]] = {}
    keys = {"artist", "albumartist", "artists", "display artist",
            "albumartists", "album_artist", "artists_credit", "artist_credit",
            "artists_sort", "artistsort",
            "album", "title", "date", "genre", "tracknumber"}
    try:
        items = audio.tags.items()
    except AttributeError:
        # ID3 fallback
        items = audio.tags
    for k, v in items:
        if k.lower() in keys:
            out.setdefault(k.upper(), []).extend(v)
    return out


def print_view(path: Path) -> None:
    try:
        audio = open_audio(path)
    except Exception as exc:
        print(f"[skip] {path}: {exc}")
        return
    if audio is None or audio.tags is None:
        print(f"[skip] {path}: unsupported or no tags")
        return

    print(f"\n{path}")
    tags = read_tags(audio)
    for key in ("TITLE", "ARTIST", "DISPLAY ARTIST", "ALBUMARTIST", "ARTISTS",
                "ALBUM", "DATE", "GENRE", "TRACKNUMBER"):
        if key in tags:
            for v in tags[key]:
                print(f"  {key:<14} = {v}")


def plan_changes(audio, overrides: dict[str, str],
                 albumartist_sep: str,
                 extract_feat_from_title: bool = False) -> dict | None:
    """Return a dict of planned tag edits, or None if no changes needed.

    When `extract_feat_from_title` is True, a parenthesized
    "(feat. X)"/"(ft. X)" credit in TITLE is moved into ARTIST (as
    extra values) and DISPLAY ARTIST ("Primary feat. X"), and the
    suffix is stripped from TITLE. Off by default.
    """
    if audio.tags is None:
        return None

    artist_values = list(audio.tags.get("ARTIST", []))
    if not artist_values:
        return None

    # If ARTIST already has multiple values, only re-case each entry —
    # don't re-split. This keeps the script idempotent.
    if len(artist_values) == 1:
        original_display = artist_values[0]
        parts = split_artists(original_display)
    else:
        # Already split: preserve the original display string from
        # DISPLAY ARTIST if present, otherwise reconstruct one.
        original_display = list(audio.tags.get("DISPLAY ARTIST", []))
        if len(original_display) == 1:
            original_display = original_display[0]
        else:
            original_display = artist_values[0]
        parts = list(artist_values)

    new_artist_values = [format_artist(p, overrides) for p in parts]

    # --- Optional: extract "(feat. X)" from TITLE ---------------------
    new_title_values: list[str] | None = None
    title_featured_fmt: list[str] = []
    if extract_feat_from_title:
        title_values = list(audio.tags.get("TITLE", []))
        if title_values:
            cleaned, featured_raw = extract_title_feat(title_values[0])
            if featured_raw:
                title_featured_fmt = [
                    format_artist(n, overrides) for n in featured_raw
                ]
                # Append featured artists missing from ARTIST
                # (case-insensitive de-dupe, preserve order).
                seen = {p.lower() for p in new_artist_values}
                for feat in title_featured_fmt:
                    if feat.lower() not in seen:
                        seen.add(feat.lower())
                        new_artist_values.append(feat)
                if cleaned != title_values[0]:
                    new_title_values = [cleaned, *title_values[1:]]

    changes: dict[str, list[str]] = {}

    if new_artist_values != artist_values:
        changes["ARTIST"] = new_artist_values

    # DISPLAY ARTIST (with a space — Picard/Foobar convention).
    # Default: store the original "feat./&" string verbatim.
    # With title-feat extraction: "Primary feat. X & Y".
    display_target = original_display
    if title_featured_fmt:
        if FEAT_RE.search(original_display):
            # ARTIST field already carries a feat. credit; keep its
            # display string but ensure title-derived names are visible.
            # (ARTIST values above already merged both sources.)
            pass
        else:
            display_target = (
                f"{original_display} feat. {' & '.join(title_featured_fmt)}"
            )
    current_display = list(audio.tags.get("DISPLAY ARTIST", []))
    if len(current_display) != 1 or current_display[0] != display_target:
        changes["DISPLAY ARTIST"] = [display_target]

    if new_title_values is not None:
        changes["TITLE"] = new_title_values

    # ALBUMARTIST: if multi-artist track, join with separator.
    albumartist_values = list(audio.tags.get("ALBUMARTIST", []))
    if albumartist_values and len(new_artist_values) > 1:
        aa_parts = []
        for v in albumartist_values:
            for p in split_artists(v):
                aa_parts.append(format_artist(p, overrides))
        # de-dupe while preserving order
        seen = set()
        aa_unique = []
        for p in aa_parts:
            key = p.lower()
            if key not in seen:
                seen.add(key)
                aa_unique.append(p)
        new_aa = [albumartist_sep.join(aa_unique)]
        if new_aa != albumartist_values:
            changes["ALBUMARTIST"] = new_aa

    return changes or None


def apply_changes(audio, changes: dict) -> None:
    for key, values in changes.items():
        audio.tags[key] = values
    audio.save()


AUDIO_SUFFIXES = (".flac", ".mp3", ".ogg", ".oga")


def collect_files(paths: list[Path]) -> list[Path]:
    """Expand a list of files/dirs into a sorted list of audio files."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in AUDIO_SUFFIXES:
                    out.append(child)
        elif p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES:
            out.append(p)
    return out


def plan_file(path: Path, overrides: dict[str, str],
              albumartist_sep: str,
              extract_feat_from_title: bool = False) -> dict | None:
    """Open a file, plan changes, and return a result dict.

    Returns None if the file can't be opened or has no tags.
    Returns a dict with keys:
        path: Path
        error: str  (only present if open failed but a stub was returned)
        original: {tag: [values]} for tags we care about
        changes: {tag: [values]} planned edits (empty if nothing to do)
    """
    try:
        audio = open_audio(path)
    except Exception as exc:
        return {"path": path, "error": str(exc), "original": {}, "changes": {}}
    if audio is None or audio.tags is None:
        return {"path": path, "error": "unsupported or no tags",
                "original": {}, "changes": {}}

    original = read_tags(audio)
    changes = plan_changes(audio, overrides, albumartist_sep,
                           extract_feat_from_title) or {}
    return {"path": path, "original": original, "changes": changes}


def apply_file(path: Path, overrides: dict[str, str],
               albumartist_sep: str,
               extract_feat_from_title: bool = False) -> dict | None:
    """Open a file, apply planned changes in-place. Returns the same shape
    as plan_file, or None if nothing to apply / file unreadable."""
    try:
        audio = open_audio(path)
    except Exception as exc:
        return {"path": path, "error": str(exc), "original": {}, "changes": {}}
    if audio is None or audio.tags is None:
        return {"path": path, "error": "unsupported or no tags",
                "original": {}, "changes": {}}

    original = read_tags(audio)
    changes = plan_changes(audio, overrides, albumartist_sep,
                           extract_feat_from_title)
    if not changes:
        return {"path": path, "original": original, "changes": {}}
    apply_changes(audio, changes)
    return {"path": path, "original": original, "changes": changes}


def snapshot_tags(path: Path) -> dict[str, list[str]] | None:
    """Read all tags from the file at path. Returns None on error.

    Used to capture a backup of the original tags before Apply, so that
    Undo can restore them via write_tags().
    """
    try:
        audio = open_audio(path)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None
    return read_tags(audio)


def write_tags(path: Path, snapshot: dict[str, list[str]],
               delete: set[str] | None = None) -> bool:
    """Overwrite the file's tags with `snapshot`. Tags listed in `delete`
    are removed (used to strip tags we previously added but want gone).
    Returns True on success."""
    try:
        audio = open_audio(path)
    except Exception:
        return False
    if audio is None or audio.tags is None:
        return False
    for key, values in snapshot.items():
        audio.tags[key] = values
    for key in delete or set():
        if key in audio.tags:
            del audio.tags[key]
    audio.save()
    return True


def cmd_view(paths: list[Path]) -> int:
    rc = 0
    for p in paths:
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in (
                    ".flac", ".mp3", ".ogg", ".oga"):
                    print_view(child)
        else:
            print_view(p)
    return rc


def cmd_fix(paths: list[Path], apply: bool, albumartist_sep: str,
            extract_feat_from_title: bool = False) -> int:
    overrides = load_overrides()
    files = []
    for p in paths:
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in (
                    ".flac", ".mp3", ".ogg", ".oga"):
                    files.append(child)
        else:
            files.append(p)

    modified = 0
    for f in files:
        try:
            audio = open_audio(f)
        except Exception as exc:
            print(f"[skip] {f}: {exc}")
            continue
        if audio is None or audio.tags is None:
            print(f"[skip] {f}: unsupported or no tags")
            continue

        changes = plan_changes(audio, overrides, albumartist_sep,
                               extract_feat_from_title)
        if changes is None:
            continue

        print(f"\n{f}")
        for k, v in changes.items():
            print(f"  {k}:")
            for line in v:
                print(f"    + {line}")
        if apply:
            apply_changes(audio, changes)
            modified += 1
            print("  -> written")
        else:
            print("  (dry-run; pass --apply to write)")

    print(f"\n{'Wrote' if apply else 'Would write'} {modified} file(s).")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tagsplice")
    sub = parser.add_subparsers(dest="cmd", required=True)

    view_p = sub.add_parser("view", help="Print tags for files")
    view_p.add_argument("paths", nargs="+", type=Path)

    fix_p = sub.add_parser("fix", help="Normalize ARTIST and ALBUMARTIST")
    fix_p.add_argument("paths", nargs="+", type=Path)
    fix_p.add_argument("--apply", action="store_true",
                       help="Write changes back to files")
    fix_p.add_argument("--albumartist-sep", default=" ; ",
                       help="Separator used in ALBUMARTIST for multi-artist tracks")
    fix_p.add_argument("--extract-feat-from-title", action="store_true",
                       help="Move '(feat. X)'/'(ft. X)' from TITLE into "
                            "ARTIST (extra values) and DISPLAY ARTIST "
                            "('Primary feat. X'), and strip it from TITLE. "
                            "Off by default.")

    args = parser.parse_args(argv)
    if args.cmd == "view":
        return cmd_view(args.paths)
    if args.cmd == "fix":
        return cmd_fix(args.paths, args.apply, args.albumartist_sep,
                       args.extract_feat_from_title)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
