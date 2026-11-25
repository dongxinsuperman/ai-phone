"""Compare two in-memory mobile observations. Target IDs belong to a single frame."""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass

from ai_phone.shared.android_frame_regions import changed_bounds


_MAX_ITEMS = 6
_MAX_LABEL = 96
_STATE_EVIDENCE = {
    "selected": ("node", "owned_name_node"),
    "checked": ("node", "owned_checkable_node"),
    "focused": ("node",),
    "enabled": ("node",),
    "expanded": ("node",),
}


def _short(text: str) -> str:
    return text if len(text) <= _MAX_LABEL else text[: _MAX_LABEL - 1] + "…"


@dataclass
class _Read:
    words: dict[str, None]
    complete: bool


def _word_list(values: list) -> dict[str, None] | None:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return dict.fromkeys(text for value in values if (text := " ".join(value.split())))


def _located_words(rows, frame: dict) -> dict[str, None] | None:
    if not isinstance(rows, list):
        return None
    width, height = frame.get("width"), frame.get("height")
    if any(dimension is not None and (type(dimension) is not int or dimension <= 0) for dimension in (width, height)):
        return None
    words = []
    for row in rows:
        bounds = row.get("bounds") if isinstance(row, dict) else None
        if not isinstance(bounds, (tuple, list)) or len(bounds) != 4 or not all(type(v) is int for v in bounds):
            return None
        left, top, right, bottom = bounds
        if not (0 <= left < right and 0 <= top < bottom):
            return None
        if (width is not None and right > width) or (height is not None and bottom > height):
            return None
        words.append(row.get("text"))
    return _word_list(words)


def _reads(frame: dict, side: str, warnings: list[str]) -> dict[str, _Read]:
    reads = {}
    if frame.get("tree_available") is True:
        if "system_texts" in frame or "screen_texts" in frame:
            # New Agents merge both readers into `texts`; only the located records preserve provenance.
            words = _located_words(frame.get("system_texts"), frame)
        else:
            words = _word_list(frame.get("texts"))
        if words is None:
            warnings.append(f"{side}_texts_unavailable")
        else:
            complete = not frame.get("system_texts_truncated", frame.get("texts_truncated", False))
            reads["system"] = _Read(words, complete)
            if not complete:
                warnings.append(f"{side}_texts_truncated")
    else:
        warnings.append(f"{side}_tree_unavailable")

    if "screen_texts_unavailable" in (frame.get("warnings") or ()) or frame.get("screen_texts_available") is False:
        warnings.append(f"{side}_screen_texts_unavailable")
    elif "screen_texts" in frame or "system_texts" in frame:
        words = _located_words(frame.get("screen_texts"), frame)
        if words is None:
            warnings.append(f"{side}_screen_texts_unavailable")
        else:
            complete = not frame.get("screen_texts_truncated", False)
            reads["screen"] = _Read(words, complete)
            if not complete:
                warnings.append(f"{side}_screen_texts_truncated")
    return reads


def _preview(values: list[str], frame: dict) -> list[str]:
    targets = frame.get("targets")
    targets = targets if isinstance(targets, list) else []
    names = {name for item in targets if isinstance(item, dict) and isinstance((name := item.get("name")), str)}
    ordered = [v for v in values if v in names] + [v for v in values if v not in names]
    return [_short(v) for v in ordered[:_MAX_ITEMS]]


def _difference(
    seen: dict | None, baseline: dict | None, seen_complete: bool, baseline_complete: bool
) -> list[str]:
    if seen is None or baseline is None or not baseline_complete:
        return []
    observed = [text for text in seen if text not in baseline]
    if seen_complete:
        return observed
    # The truncation flag covers clipped strings as well as omitted rows. A prefix might be unchanged.
    reference = sorted(baseline)
    result = []
    for text in observed:
        index = bisect_left(reference, text)
        if index == len(reference) or not reference[index].startswith(text):
            result.append(text)
    return result


def _observed_difference(reads: dict[str, _Read], baseline: dict[str, _Read], common: list[str]) -> list[str]:
    if not common:
        return []
    current = dict.fromkeys(word for source in common for word in reads[source].words)
    # Switching readers for the same visible word must never generate an addition or removal.
    known = dict.fromkeys(word for read in baseline.values() for word in read.words)
    return _difference(current, known, all(reads[s].complete for s in common), all(baseline[s].complete for s in common))


def _targets(frame: dict, side: str, warnings: list[str]) -> list[dict] | None:
    if frame.get("tree_available") is not True:
        return None
    targets = frame.get("targets")
    if not isinstance(targets, list) or not all(isinstance(item, dict) for item in targets):
        warnings.append(f"{side}_targets_unavailable")
        return None
    if frame.get("targets_truncated"):
        # A second control with the same name or resource ID may have been omitted.
        warnings.append(f"{side}_targets_truncated")
        return None
    platform = frame.get("platform", "android")
    return [item for item in targets if item.get("source") in (platform, "system")]


def _label(target: dict, field: str) -> str:
    value = target.get(field)
    return value.strip() if isinstance(value, str) else ""


@dataclass
class _ControlNames:
    system: dict[str, list[set]]
    all_names: set[str]
    unnamed_locations: set
    sources_known: bool


def _control_names(targets: list[dict], frame: dict) -> _ControlNames:
    system: dict[str, list[set]] = {}
    unnamed_locations = set()
    all_names = {name for t in frame["targets"] if (name := _label(t, "name"))}
    sources_known = True
    for target in targets:
        if not target.get("actions"):
            continue
        name, origin = _label(target, "name"), _label(target, "name_source")
        locations = set()
        if resource := _label(target, "resource_id"):
            locations.add(("resource", resource))
        box = target.get("bounds")
        if isinstance(box, (list, tuple)) and len(box) == 4 and all(type(x) is int for x in box):
            locations.add(("bounds", _label(target, "role"), tuple(box)))
        # Older observation Agents put the XML field itself in name_source.
        direct = origin.removeprefix("descendant_")
        if name and (origin == "system" or direct in {"text", "content-desc", "hint"}):
            system.setdefault(name, []).append(locations)
        else:
            unnamed_locations.update(locations)
            if (name and origin != "screen") or not locations:
                sources_known = False
    return _ControlNames(system, all_names, unnamed_locations, sources_known)


def _control_name_difference(seen: _ControlNames, baseline: _ControlNames, visible_delta: list[str]) -> list[str]:
    if not baseline.sources_known:
        return []
    already_reported = set(visible_delta)
    return [name for name, locations in seen.system.items()
            if name not in baseline.all_names and name not in already_reported
            and not any(location & baseline.unnamed_locations for location in locations)]


def _identifiable(targets: list[dict]) -> dict[tuple, dict]:
    names = Counter(_label(t, "name") for t in targets)
    resources = Counter(_label(t, "resource_id") for t in targets)
    matched = {}
    for target in targets:
        name, resource = _label(target, "name"), _label(target, "resource_id")
        if name and names[name] != 1:
            continue
        if not name and (not resource or resources[resource] != 1):
            continue
        box = target.get("bounds")
        if not isinstance(box, (tuple, list)) or len(box) != 4 or not all(type(n) is int for n in box):
            continue
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        role = _label(target, "role")
        actions = target.get("actions", [])
        if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
            continue
        origin = target.get("name_source")
        name_origin = origin if origin in {"screen", "webview"} else "system"
        key = (name, resource, role, _label(target, "class"), tuple(sorted(actions)), name_origin)
        matched[key] = target
    return matched


def _same_position(previous: dict, current: dict) -> bool:
    a, b = previous["bounds"], current["bounds"]
    path = previous.get("tree_path")
    if path != current.get("tree_path"):
        return False
    if tuple(a) == tuple(b):
        return True
    # A settling panel can move a few pixels. Require the same tree position and near-identical bounds.
    if not path or max(abs(x - y) for x, y in zip(a, b)) > 8:
        return False
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    overlap = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap >= 0.95 * union


def _state_changes(before: list[dict], after: list[dict]) -> list[dict]:
    old = _identifiable(before)
    result = []
    for identity, current in _identifiable(after).items():
        previous = old.get(identity)
        if previous is None or not _same_position(previous, current):
            continue
        a, b = previous.get("state"), current.get("state")
        old_evidence, new_evidence = previous.get("state_evidence"), current.get("state_evidence")
        if not all(isinstance(x, dict) for x in (a, b, old_evidence, new_evidence)):
            continue
        for prop, allowed in _STATE_EVIDENCE.items():
            evidence = old_evidence.get(prop)
            if evidence not in allowed or new_evidence.get(prop) != evidence:
                continue
            left, right = a.get(prop), b.get(prop)
            if type(left) is bool and type(right) is bool and left != right:
                result.append(
                    {
                        "id": current.get("id"),
                        "name": _short(identity[0] or identity[1]),
                        "property": prop,
                        "before": left,
                        "after": right,
                    }
                )
    return result


def _changed_regions(previous: dict, current: dict, reads: dict[str, _Read]) -> list[dict] | None:
    width, height = current.get("width"), current.get("height")
    if not all(type(value) is int and value > 0 for value in (width, height)):
        return None
    if any(previous.get(k) != current.get(k) for k in ("width", "height", "rotation")):
        return None
    boxes = changed_bounds(previous.get("_raw_grid_v1"), current.get("_raw_grid_v1"), width, height)
    if not boxes:
        return boxes
    # Only readers already validated for this capture can locate a name in its changed area.
    labels = [row for source in reads for row in current.get(f"{source}_texts", [])]
    regions = []
    for left, top, right, bottom in boxes:
        inside = {}
        for row in labels:
            x1, y1, x2, y2 = row["bounds"]
            if left <= x1 < x2 <= right and top <= y1 < y2 <= bottom:
                inside[_short(row["text"])] = None
        region = {"bounds": [left, top, right, bottom]}
        if inside:
            region["texts_in_region"] = list(inside)[:3]
        regions.append(region)
    return regions


def changes(previous: dict, current: dict) -> dict:
    """Report witnessed text and control-state differences; unknown text totals are null.

    A word in a partial current tree can prove an addition against a complete old tree.
    Its absence there cannot prove removal. Frame-local UI IDs never establish identity.
    """
    warnings: list[str] = []
    old_reads = _reads(previous, "previous", warnings)
    new_reads = _reads(current, "current", warnings)
    common = [source for source in old_reads if source in new_reads]
    if old_reads and new_reads and old_reads.keys() != new_reads.keys():
        warnings.append("text_sources_changed")
    before = _targets(previous, "previous", warnings)
    after = _targets(current, "current", warnings)
    added = _observed_difference(new_reads, old_reads, common)
    removed = _observed_difference(old_reads, new_reads, common)
    complete = bool(common) and old_reads.keys() == new_reads.keys() and all(
        old_reads[source].complete and new_reads[source].complete for source in common
    )
    # A shared coordinate and label can belong to different windows or display geometries.
    scope_keys = ("platform", "package", "activity", "width", "height", "rotation")
    scope_changed = any(previous.get(k) != current.get(k) for k in scope_keys)
    if scope_changed:
        warnings.append("state_comparison_context_changed")
    comparable = before is not None and after is not None and not scope_changed
    states = _state_changes(before, after) if comparable else []
    result = {
        "added_text": _preview(added, current),
        "removed_text": _preview(removed, previous),
        "added_text_count": len(added) if complete else None,
        "removed_text_count": len(removed) if complete else None,
        "state_changes": states[:_MAX_ITEMS],
        "state_changes_count": len(states) if comparable else None,
        "warnings": warnings,
    }
    if any(k in f for f in (previous, current) for k in ("system_texts", "screen_texts")):
        result["text_sources"] = common
    added_names, removed_names = [], []
    if before is not None and after is not None:
        old_names, new_names = _control_names(before, previous), _control_names(after, current)
        added_names = _control_name_difference(new_names, old_names, added)
        removed_names = _control_name_difference(old_names, new_names, removed)
        if not old_names.sources_known or not new_names.sources_known:
            result["target_names_incomplete"] = True
    else:
        result["target_names_incomplete"] = True
    result["added_target_names"] = [_short(name) for name in added_names[:_MAX_ITEMS]]
    result["removed_target_names"] = [_short(name) for name in removed_names[:_MAX_ITEMS]]
    if max(len(added_names), len(removed_names)) > _MAX_ITEMS:
        result["added_target_name_count"] = len(added_names)
        result["removed_target_name_count"] = len(removed_names)
    regions = _changed_regions(previous, current, new_reads)
    if regions is not None:
        result["changed_regions"] = regions
    return result
