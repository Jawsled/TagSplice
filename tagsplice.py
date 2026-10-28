#!/usr/bin/env python3
"""
TagSplice - view and normalize ID3/Vorbis tags for audio files.

Normalizations:
  * ARTIST: split "Primary feat. X & Y" into multiple ARTIST values,
    one per artist, with title-cased names (overrides + str.title() fallback).
  * ALBUMARTIST: when multiple artists are listed, join them with " ; "
    (the standard multi-value separator for album-artist groupings).
  * DISPLAY ARTIST: default to "A, B & C" notation, rebuilt when the
    stored value still contains raw ";" / "//" separators.
  * TITLE credit extraction (OPTIONAL, off by default): move
    "(feat. X)" / "(ft. X)" / "[feat. X]" suffixes from TITLE into
    ARTIST and DISPLAY ARTIST and strip them from TITLE. Remix credits
    like "(DJ X Remix)" / "[DJ X Remix]" only fill REMIXER (TPE4 for
    MP3); the TITLE and DISPLAY ARTIST are left untouched. Enable with
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
from mutagen.id3 import (
    ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPE4, TRCK, TSOP, TXXX,
)
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

# Parenthesized/bracketed remix credit inside TITLE, e.g.
#   "Song (DJ X Remix)", "Song [DJ X Rmx]", or "Song - DJ Y Remix".
TITLE_REMIX_RE = re.compile(
    r"\s*[\(\[]\s*([^\)\]]*?(?:remix|rmx)[^\)\]]*?)\s*[\)\]]",
    re.IGNORECASE,
)
# Trailing "Song - DJ X Remix" form.
TITLE_REMIX_DASH_RE = re.compile(
    r"\s+[-–—]\s+([^-–—\(\)\[\]]*?(?:remix|rmx)"
    r"[^-–—\(\)\[\]]*?)\s*$",
    re.IGNORECASE,
)
REMIX_WORD_RE = re.compile(r"\b(?:remix|rmx)\b", re.IGNORECASE)
REMIX_BY_RE = re.compile(
    r"^(?:the\s+)?remix\s+by\s+(.+)$", re.IGNORECASE)

# Credit words that describe a remix type rather than a remixer.
REMIX_GENERIC_WORDS = {
    "remix", "rmx", "mix", "edit", "version", "extended", "radio",
    "club", "dub", "vocal", "instrumental", "original", "album",
    "single", "short", "long", "main", "bonus", "bootleg", "mashup",
    "acoustic", "live", "demo", "nightcore", "slowed", "reverb",
    "lofi", "lo-fi", "the", "a", "an",
}

# Logical tag name -> ID3 frame for writing. Vorbis tags use the
# logical name directly. DISPLAY ARTIST etc. are stored in TXXX frames.
ID3_TEXT_FRAMES = {
    "TITLE": ("TIT2", TIT2),
    "ARTIST": ("TPE1", TPE1),
    "ALBUMARTIST": ("TPE2", TPE2),
    "REMIXER": ("TPE4", TPE4),
    "ALBUM": ("TALB", TALB),
    "DATE": ("TDRC", TDRC),
    "GENRE": ("TCON", TCON),
    "TRACKNUMBER": ("TRCK", TRCK),
    "ARTISTSORT": ("TSOP", TSOP),
}
ID3_TXXX_TAGS = {"DISPLAY ARTIST", "ARTISTS", "ALBUMARTISTS"}


def load_override_rules(path: Path = OVERRIDES_PATH) -> dict[str, str]:
    """Load override rules exactly as stored (key casing preserved).

    Returns {} when the file does not exist. Raises ValueError when the
    file cannot be read or is not a JSON object of "artist": "display"
    pairs, so the GUI can explain why rules are not being applied.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} must contain a JSON object of "
            '"artist": "display" pairs.')
    return {str(k): str(v) for k, v in data.items()}


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, str]:
    """Load overrides keyed by lowercased artist name for matching."""
    return {k.lower(): v for k, v in load_override_rules(path).items()}


def save_override_rules(rules: dict[str, str],
                        path: Path = OVERRIDES_PATH) -> None:
    """Write `rules` to `path` as a plain JSON object.

    The file stays hand-editable and usable by the CLI. Blank rows are
    dropped; a rule missing either side, or a duplicate (case-insensitive)
    artist name, raises ValueError. The write is atomic.
    """
    clean: dict[str, str] = {}
    seen: dict[str, str] = {}
    for raw_key, raw_value in rules.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key and not value:
            continue
        if not key or not value:
            raise ValueError(
                f'Rule for "{key or value}" needs both an artist and a '
                "display name.")
        lowered = key.lower()
        if lowered in seen:
            raise ValueError(
                f'Duplicate rule for "{key}" (already set by '
                f'"{seen[lowered]}"); matching ignores case.')
        seen[lowered] = key
        clean[key] = value
    payload = dict(sorted(clean.items(), key=lambda kv: kv[0].lower()))
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _split_on_separators(text: str) -> list[str]:
    """Split a raw credit on '//', ';', '&' and ',', dropping empties."""
    out: list[str] = []
    for piece in EDIT_SEP_RE.split(text):
        for piece in SEMICOLON_RE.split(piece):
            for piece in AMP_RE.split(piece):
                for piece in COMMA_RE.split(piece):
                    piece = piece.strip()
                    if piece:
                        out.append(piece)
    return out


def split_artists(artist_field: str) -> list[str]:
    """Split 'Primary feat. X & Y' into ['Primary', 'X', 'Y'].

    Handles all common in-band separators automatically:
      '//' (TagSplice multi-value marker), ';', 'feat./ft./featuring',
      '&', and ','. Surrounding whitespace is trimmed and empty
      segments are dropped.
    """
    # Split feat. off first so 'A feat. B & C' -> ['A', 'B & C'].
    parts = FEAT_RE.split(artist_field, maxsplit=1)
    out: list[str] = []
    for chunk in parts:
        out.extend(_split_on_separators(chunk))
    return out or [artist_field]


def split_feat(artist_field: str) -> tuple[list[str], list[str]]:
    """Split an ARTIST value into (primary_names, featured_names)."""
    parts = FEAT_RE.split(artist_field, maxsplit=1)
    primary = _split_on_separators(parts[0])
    featured = _split_on_separators(parts[1]) if len(parts) == 2 else []
    return primary, featured


def join_display(names: list[str]) -> str:
    """Join names as 'A', 'A & B', or 'A, B & C'."""
    clean = [n.strip() for n in names if n and n.strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} & {clean[1]}"
    return ", ".join(clean[:-1]) + f" & {clean[-1]}"


def format_display(primary: list[str], featured: list[str] | None = None) -> str:
    """Build a display credit: 'A, B & C feat. X & Y' (featured last)."""
    base = join_display(primary)
    if featured:
        feat_text = join_display(featured)
        base = f"{base} feat. {feat_text}" if base else f"feat. {feat_text}"
    return base


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


def _remixers_from_credit(credit: str) -> list[str]:
    """Pull remixer names out of a single remix credit.

    'DJ X Remix' -> ['DJ X']; 'Extended Remix' -> [] (a remix type,
    not a remixer); 'Remix by DJ X' -> ['DJ X'].
    """
    by_match = REMIX_BY_RE.match(credit.strip())
    source = by_match.group(1) if by_match else credit
    names: list[str] = []
    for chunk in _split_on_separators(source):
        chunk = REMIX_WORD_RE.split(chunk, maxsplit=1)[0]
        chunk = chunk.strip(" -–—,;:&").strip()
        chunk = re.sub(r"['’]s$", "", chunk, flags=re.IGNORECASE).strip()
        if chunk and chunk.lower() not in REMIX_GENERIC_WORDS:
            names.append(chunk)
    return names


def extract_title_remix(title: str) -> list[str]:
    """Collect remixer names from remix credits in a TITLE.

    Unlike feat. credits, remix credits stay in TITLE (they are part of
    the track name) and are not added to DISPLAY ARTIST. Both
    parenthesized and bracketed forms are recognized, and the title is
    never modified.
    """
    credits: list[str] = []

    def collect(match: re.Match) -> None:
        credit = re.sub(r"\s+", " ", match.group(1)).strip()
        if credit:
            credits.append(credit)

    for match in TITLE_REMIX_RE.finditer(title):
        collect(match)
    for match in TITLE_REMIX_DASH_RE.finditer(title):
        collect(match)

    remixers: list[str] = []
    for credit in credits:
        remixers.extend(_remixers_from_credit(credit))
    return remixers


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


def _id3_encoding(tags: ID3) -> int:
    """UTF-8 on ID3v2.4, UTF-16 (v2.3-compatible) otherwise."""
    return 3 if getattr(tags, "version", (2, 4)) >= (2, 4) else 1


def get_tag_values(audio, key: str) -> list[str]:
    """Read a logical tag (ARTIST, DISPLAY ARTIST, REMIXER, ...)."""
    tags = audio.tags
    if tags is None:
        return []
    if isinstance(tags, ID3):
        if key in ID3_TXXX_TAGS:
            frame = tags.get(f"TXXX:{key}")
        else:
            spec = ID3_TEXT_FRAMES.get(key)
            frame_id = spec[0] if spec else f"TXXX:{key}"
            frame = tags.get(frame_id)
        if frame is None:
            return []
        return [str(v) for v in getattr(frame, "text", [])]
    return [str(v) for v in tags.get(key, [])]


def set_tag_values(audio, key: str, values: list[str]) -> None:
    """Write a logical tag, mapping to TPE1/TPE4/TXXX on ID3."""
    tags = audio.tags
    if tags is None:
        return
    values = [str(v) for v in values]
    if isinstance(tags, ID3):
        encoding = _id3_encoding(tags)
        if key in ID3_TXXX_TAGS:
            tags.delall(f"TXXX:{key}")
            if values:
                tags.add(TXXX(encoding=encoding, desc=key, text=values))
            return
        spec = ID3_TEXT_FRAMES.get(key)
        if spec is None:
            tags.delall(f"TXXX:{key}")
            if values:
                tags.add(TXXX(encoding=encoding, desc=key, text=values))
            return
        frame_id, frame_cls = spec
        tags.delall(frame_id)
        if values:
            tags.add(frame_cls(encoding=encoding, text=values))
        return
    tags[key] = values


def delete_tag(audio, key: str) -> None:
    tags = audio.tags
    if tags is None:
        return
    if isinstance(tags, ID3):
        spec = ID3_TEXT_FRAMES.get(key)
        if key in ID3_TXXX_TAGS or spec is None:
            tags.delall(f"TXXX:{key}")
        else:
            tags.delall(spec[0])
        return
    if key in tags:
        del tags[key]


def read_tags(audio) -> dict[str, list[str]]:
    """Return tag -> [values] for the keys we care about.

    ID3 files are mapped from frames (TPE1, TPE4, TXXX:...) to logical
    names; Vorbis keys are case-insensitive and normalized to uppercase.
    """
    out: dict[str, list[str]] = {}
    if isinstance(audio.tags, ID3):
        for key in (*ID3_TEXT_FRAMES, *sorted(ID3_TXXX_TAGS)):
            values = get_tag_values(audio, key)
            if values:
                out[key] = values
        return out

    keys = {"artist", "albumartist", "artists", "display artist",
            "albumartists", "album_artist", "artists_credit", "artist_credit",
            "artists_sort", "artistsort", "remixer",
            "album", "title", "date", "genre", "tracknumber"}
    try:
        items = audio.tags.items()
    except AttributeError:
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
    for key in ("TITLE", "ARTIST", "DISPLAY ARTIST", "REMIXER",
                "ALBUMARTIST", "ARTISTS",
                "ALBUM", "DATE", "GENRE", "TRACKNUMBER"):
        if key in tags:
            for v in tags[key]:
                print(f"  {key:<14} = {v}")


def plan_changes(audio, overrides: dict[str, str],
                 albumartist_sep: str,
                 extract_feat_from_title: bool = False) -> dict | None:
    """Return a dict of planned tag edits, or None if no changes needed.

    When `extract_feat_from_title` is True, parenthesized
    "(feat. X)"/"(ft. X)" credits in TITLE are moved into ARTIST and
    DISPLAY ARTIST and stripped from TITLE. Remix credits such as
    "(DJ X Remix)"/"[DJ X Remix]" only set REMIXER: the TITLE and
    DISPLAY ARTIST are left untouched. Off by default.
    """
    if audio.tags is None:
        return None

    artist_values = get_tag_values(audio, "ARTIST")
    if not artist_values:
        return None

    # Split feat. credits from ARTIST. A single value is fully split on
    # in-band separators; existing multi-value ARTIST tags are treated
    # as already split so group names like "Simon & Garfunkel" survive.
    primary_parts: list[str] = []
    artist_featured: list[str] = []
    if len(artist_values) == 1:
        primary_parts, artist_featured = split_feat(artist_values[0])
    else:
        for value in artist_values:
            parts = FEAT_RE.split(value, maxsplit=1)
            primary = parts[0].strip()
            if primary:
                primary_parts.append(primary)
            if len(parts) == 2:
                artist_featured.extend(_split_on_separators(parts[1]))

    formatted_primary = [format_artist(p, overrides) for p in primary_parts]
    formatted_featured = [format_artist(n, overrides) for n in artist_featured]
    new_artist_values = [*formatted_primary, *formatted_featured]
    if not new_artist_values:
        return None

    # --- Optional: extract feat./remix credits from TITLE -------------
    new_title_values: list[str] | None = None
    title_featured: list[str] = []
    remixer_fmt: list[str] = []
    if extract_feat_from_title:
        title_values = get_tag_values(audio, "TITLE")
        if title_values:
            original_title = title_values[0]
            cleaned, featured_raw = extract_title_feat(original_title)
            # Remix credits are read from the untouched title and only
            # feed REMIXER; they stay in TITLE and out of DISPLAY ARTIST.
            remixer_raw = extract_title_remix(original_title)
            if featured_raw:
                title_featured = [
                    format_artist(n, overrides) for n in featured_raw
                ]
                # Append featured artists missing from ARTIST
                # (case-insensitive de-dupe, preserve order).
                seen = {p.lower() for p in new_artist_values}
                for feat in title_featured:
                    if feat.lower() not in seen:
                        seen.add(feat.lower())
                        new_artist_values.append(feat)
            if remixer_raw:
                remixer_fmt = [format_artist(n, overrides)
                               for n in remixer_raw]
            if cleaned != original_title:
                new_title_values = [cleaned, *title_values[1:]]

    changes: dict[str, list[str]] = {}

    if new_artist_values != artist_values:
        changes["ARTIST"] = new_artist_values

    # DISPLAY ARTIST (with a space — Picard/Foobar convention).
    # Keep a curated existing value, but rebuild it as "A, B & C"
    # when it is missing, still contains raw ";" / "//" separators, or
    # merely repeats one artist from a multi-artist track (the old bug).
    current_display = get_tag_values(audio, "DISPLAY ARTIST")
    artist_keys = {p.lower() for p in new_artist_values}
    curated_display = (
        len(current_display) == 1
        and current_display[0].strip()
        and not SEMICOLON_RE.search(current_display[0])
        and EDIT_SEP not in current_display[0]
        and not (len(new_artist_values) > 1
                 and current_display[0].strip().lower() in artist_keys)
    )
    if curated_display:
        display_target = current_display[0].strip()
    else:
        display_target = format_display(formatted_primary,
                                        formatted_featured)
    if title_featured and not FEAT_RE.search(display_target):
        display_target = (
            f"{display_target} feat. {join_display(title_featured)}"
        )
    if len(current_display) != 1 or current_display[0] != display_target:
        changes["DISPLAY ARTIST"] = [display_target]

    # REMIXER / TPE4: keep existing names and add those from the title.
    current_remixers = get_tag_values(audio, "REMIXER")
    if remixer_fmt:
        seen = {r.lower() for r in current_remixers}
        new_remixers = list(current_remixers)
        for name in remixer_fmt:
            if name.lower() not in seen:
                seen.add(name.lower())
                new_remixers.append(name)
        if new_remixers != current_remixers:
            changes["REMIXER"] = new_remixers

    if new_title_values is not None:
        changes["TITLE"] = new_title_values

    # ALBUMARTIST: if multi-artist track, join with separator.
    albumartist_values = get_tag_values(audio, "ALBUMARTIST")
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
        set_tag_values(audio, key, values)
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
        set_tag_values(audio, key, values)
    for key in delete or set():
        delete_tag(audio, key)
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
    try:
        overrides = load_overrides()
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
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
    fix_p.add_argument("--extract-feat-from-title",
                       "--extract-title-credits",
                       dest="extract_feat_from_title",
                       action="store_true",
                       help="Move '(feat. X)'/'(ft. X)' from TITLE into "
                            "ARTIST and DISPLAY ARTIST and strip them "
                            "from TITLE. Remix credits like "
                            "'(DJ X Remix)'/'[DJ X Remix]' only fill "
                            "REMIXER (TPE4 for MP3); TITLE and DISPLAY "
                            "ARTIST are left untouched. Off by default.")

    args = parser.parse_args(argv)
    if args.cmd == "view":
        return cmd_view(args.paths)
    if args.cmd == "fix":
        return cmd_fix(args.paths, args.apply, args.albumartist_sep,
                       args.extract_feat_from_title)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
