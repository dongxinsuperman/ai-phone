"""Reconcile visible glyphs and positioned native labels from the same capture."""

from __future__ import annotations

import unicodedata


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", ()))


def _word_key(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _same_printed_word(report, read):
    a, b = _word_key(report), _word_key(read)
    if a == b:
        return bool(a)
    # One confused glyph in the same multi-character word; a generic image description is insufficient.
    return len(a) == len(b) and len(a) >= 4 and sum(x != y for x, y in zip(a, b)) == 1


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _same_region(image, line):
    line_area = (line[2] - line[0]) * (line[3] - line[1])
    image_area = (image[2] - image[0]) * (image[3] - image[1])
    return _contains(image, line) and line_area >= .18 * image_area


def _printed(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _text_candidates(targets):
    return [t for t in targets if t.get("role") == "visible_text" and t.get("source") == "pixels"
            and t.get("name_source") == "screen" and "try_tap" in t.get("actions", ())
            and t.get("name") and t.get("bounds")]


def prefer_label_in_existing_target(views, observation):
    candidates = _text_candidates(observation["targets"])
    if not observation["screen_texts"]:
        return
    images = [node for node in _walk(views) if node.get("role") in {"image", "icon"}
              and node.get("name") and node.get("name_source") == "system" and node.get("bounds")
              and not node.get("target") and not node.get("children") and node.get("state", {}).get("enabled") is not False]
    replaced = set()
    for target in candidates:
        nearby = [image for image in images if not image.get("target") and _same_region(image["bounds"], target["bounds"])]
        hits = [image for image in nearby if _same_printed_word(image["name"], target["name"])]
        if len(nearby) != 1 or len(hits) != 1:
            continue
        image = hits[0]
        if sum(_contains(image["bounds"], other["bounds"]) for other in candidates) != 1:
            continue
        ident = target["id"]
        old = target["name"]
        if not any(r.get("target") == ident and r.get("text") == old and r.get("bounds") == target["bounds"]
                   for r in observation["screen_texts"]):
            continue
        image["target"] = ident
        target.update(name=image["name"], name_source="system", name_bounds=list(image["bounds"]),
                      system_name_source="native_image_label")
        observation["system_texts"].append({"text": image["name"], "source": "system",
                                           "bounds": list(image["bounds"]), "target": ident})
        replaced.add((ident, old, tuple(target["bounds"])))
    observation["screen_texts"] = [r for r in observation["screen_texts"]
                                   if (r.get("target"), r["text"], tuple(r["bounds"])) not in replaced]
    reconcile_static_paragraphs(observation)


def reconcile_static_paragraphs(observation, *, retain_readings=False):
    """A pixel transcription inside a matching static paragraph provides no independent action."""
    # A legible portion of a longer, already positioned paragraph is one piece of its text.
    # Full-width punctuation in platform text and ASCII in the pixel transcription are equivalent.
    paragraphs = [(_printed(r["text"]), r["bounds"]) for r in observation["system_texts"] if not r.get("target")]
    if not paragraphs or not observation["screen_texts"]:
        return
    pixel_ids = {t["id"] for t in _text_candidates(observation["targets"])}
    readings = [(r, _printed(r["text"])) for r in observation["screen_texts"] if not r.get("target")
                or r["target"] in pixel_ids]
    duplicates = set()
    for full, box in paragraphs:
        local = [(r, word) for r, word in readings if len(word) >= 2 and len(full) >= len(word) * 1.4
                 and _contains(box, r["bounds"])]
        anchors = [r["bounds"] for r, word in local if len(word) >= 3 and word in full]
        # Two separately located matches establish that a long system paragraph is being read in pieces.
        supported = any(a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
                        for i, a in enumerate(anchors) for b in anchors[i+1:])
        for row, word in local:
            if word in full and (len(word) >= 3 or supported):
                duplicates.add(id(row))
            elif supported:
                # Clipped pixel runs may start or end with an extra punctuation stroke.
                start = 1 if unicodedata.category(word[0]).startswith("P") else 0
                end = len(word) - int(unicodedata.category(word[-1]).startswith("P"))
                content = word[start:end]
                if len(content) >= 4 and content in full:
                    duplicates.add(id(row))
    if duplicates:
        redundant_targets = {r.get("target") for r, _ in readings if id(r) in duplicates} & pixel_ids
        observation["targets"][:] = [t for t in observation["targets"] if t["id"] not in redundant_targets]
        if retain_readings:
            for row, _ in readings:
                if row.get("target") in redundant_targets:
                    row["target"] = None
        else:
            observation["screen_texts"] = [r for r in observation["screen_texts"] if id(r) not in duplicates]


def hide_unpainted_static_text(image, views, observation):
    """An exactly uniform rectangle has no drawn lettering. Keep accessibility names on controls."""
    unowned = {(r["text"], tuple(r["bounds"])) for r in observation["system_texts"] if not r.get("target")}
    if not unowned or all(lo == hi for lo, hi in image.getextrema()):
        return
    blank = {}
    hidden = set()
    for node in _walk(views):
        if node.get("target") or node.get("role") not in {"text", "label", "title", "heading"}:
            continue
        bounds = node.get("bounds")
        if not bounds:
            continue
        box = tuple(bounds)
        for key in ("text", "value"):
            text = node.get(key)
            if (text, box) not in unowned:
                continue
            if box not in blank:
                blank[box] = all(lo == hi for lo, hi in image.crop(box).getextrema())
            if blank[box]:
                node.pop(key)
                hidden.add((text, box))
    if hidden:
        observation["system_texts"] = [r for r in observation["system_texts"]
                                       if r.get("target") or (r["text"], tuple(r["bounds"])) not in hidden]
