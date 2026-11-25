"""Reconcile a positioned native line with a second reading of the same printed glyphs."""

from collections import Counter
from functools import lru_cache
import unicodedata


def _line(text):
    if not isinstance(text, str) or len(text.splitlines()) != 1:
        return ""
    # Compatibility folding would conflate superscripts, enclosed numbers and units.
    return unicodedata.normalize("NFC", text).strip()


def _width(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


@lru_cache(maxsize=512)
def _lookalike(a, b):
    # Share the offline reader's installed fonts and its cached glyph outlines.
    # An unavailable font or a missing glyph supplies no evidence for a substitution.
    try:
        from ai_phone.agent.android_glyphs import _faces, _sized_glyph
    except ImportError:
        return False

    names = [unicodedata.name(c, "") for c in (a, b)]
    if all(n.startswith("CJK UNIFIED IDEOGRAPH-") for n in names):
        faces = _faces()[0]
    elif all(n.startswith("LATIN ") and c.isalpha() for n, c in zip(names, (a, b))) and a.isupper() == b.isupper():
        faces = _faces()[1]
    else:
        return False
    for path, index in faces:
        try:
            glyphs = [_sized_glyph(path, index, 44, 0, c) for c in (a, b, "\u0378")]
        except (OSError, ValueError):
            continue
        first, second, missing = glyphs
        if first is None or second is None:
            continue
        if missing is not None and any(float(g[0] @ missing[0]) > .999 for g in (first, second)):
            continue
        if abs(first[1] - second[1]) <= .2 and float(first[0] @ second[0]) >= .70:
            return True
    return False


def _same_reading(native, read, allow_fuzzy):
    if native == read:
        return True
    if not allow_fuzzy:
        return False
    differences = [i for i, (a, b) in enumerate(zip(native, read)) if a != b]
    if len(differences) != 1:
        return False
    i = differences[0]
    if native[i].isascii():
        # Preserve short Latin abbreviations and units embedded in a longer sentence.
        start = end = i
        while start and native[start - 1].isascii() and native[start - 1].isalpha():
            start -= 1
        while end < len(native) and native[end].isascii() and native[end].isalpha():
            end += 1
        if end - start < 4:
            return False
    return _lookalike(native[i], read[i])


def _covers(full, box, read, ink):
    if not (3 <= len(read) <= len(full)):
        return False
    x, y, right, bottom = ink
    left, top, end, base = box
    h, line_h = bottom - y, base - top
    if not (.52 * line_h <= h <= 1.15 * line_h and abs((y + bottom) - (top + base)) <= .45 * line_h):
        return False
    if x < left - .12 * h or right > end + .12 * h or right <= x:
        return False
    pitch = (right - x) / _width(read)
    span = pitch * _width(full)
    # A wide native layout may align its glyphs at the start, centre or end.
    # Project the whole line from the reading, including the offset of a partial run.
    tolerance = .65 * h
    if span > end - left + 2 * tolerance:
        return False
    origins = (left, (left + end - span) / 2, end - span)
    fuzzy = (len(read) >= 10 and len(read) * 2 >= len(full)
             and not any(c.isnumeric() or unicodedata.category(c).startswith("S") or c in "%‰" for c in full + read))
    projected_left = x
    for start in range(len(full) - len(read) + 1):
        if any(abs(projected_left - origin) <= tolerance for origin in origins):
            if _same_reading(full[start:start + len(read)], read, fuzzy):
                return True
        projected_left -= pitch * _width(full[start])
    return False


def _view_refs(nodes):
    for node in nodes:
        if node.get("source") != "pixels":
            yield node.get("target")
        if node.get("source") != "screen":
            yield node.get("belongs_to")
        yield from _view_refs(node.get("children", ()))


def remove_duplicate_screen_lines(observation, views):
    """Drop only redundant screen readings and their otherwise unreferenced OCR-only targets."""
    screen = observation["screen_texts"]
    if not screen or not observation["system_texts"]:
        return
    native = [(text, r["bounds"]) for r in observation["system_texts"]
              if r.get("bounds") and (text := _line(r.get("text")))]
    targets = {t["id"]: t for t in observation["targets"]}
    protected = {r.get("target") for r in observation["system_texts"]}
    protected.update(_view_refs([views] if isinstance(views, dict) else views or ()))
    counts = Counter(r.get("target") for r in screen)
    removed = set()
    kept = []
    for row in screen:
        ident, ink = row.get("target"), row.get("bounds")
        if ident:
            target = targets.get(ident, {})
            own_text = (ident not in protected and counts[ident] == 1 and target.get("source") == "pixels"
                        and target.get("role") == "visible_text" and target.get("name_source") == "screen"
                        and target.get("name") == row.get("text") and target.get("bounds") == ink
                        and target.get("name_bounds", ink) == ink
                        and target.get("actions") == ["try_tap"])
            if not own_text:
                kept.append(row)
                continue
        read = _line(row.get("text"))
        if ink and any(_covers(full, box, read, ink) for full, box in native):
            if ident:
                removed.add(ident)
        else:
            kept.append(row)
    screen[:] = kept
    if removed:
        observation["targets"][:] = [t for t in observation["targets"] if t["id"] not in removed]
