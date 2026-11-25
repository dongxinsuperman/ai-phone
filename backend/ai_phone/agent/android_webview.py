"""Read actionable elements from an inspectable Android WebView on the claimed transport."""

from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
import io
import json
import math
import re
import time
from urllib.parse import urlsplit

from adbutils import AdbError
from PIL import Image, ImageChops
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from . import android_observation_io as observation_io


_FOCUS = re.compile(r"mCurrentFocus=.*?\s([A-Za-z][\w.]*)/[\w.$]+")
_ENDPOINT = re.compile(r"/devtools/page/[A-Za-z0-9_-]+")
_PROCESS_SOCKETS = ("ps -A -o PID,NAME; "
                    "grep -E '@webview_devtools_remote_[0-9]+$' /proc/net/unix")
_BUDGET_SECONDS = 1.35
_MAX_MESSAGE = 8 * 1024 * 1024
_STATE_KEYS = ("checked", "selected", "expanded", "pressed", "password")

_IS_LOADING = """(() => {
    if (document.visibilityState !== "visible") return false;
    if (document.readyState !== "complete" || !document.body) return true;
    return Array.from(document.querySelectorAll('[aria-busy="true"]')).some(el => {
        const r = el.getBoundingClientRect(), style = getComputedStyle(el);
        return r.width && r.height && r.right > 0 && r.bottom > 0 &&
            r.left < innerWidth && r.top < innerHeight &&
            style.visibility === "visible" && Number(style.opacity) !== 0;
    });
})()"""
# Read only labels and positions. Focusing, scrolling and dispatching events stay with the action path.
_READ_ACTIONS = r"""(() => {
    if (document.visibilityState !== "visible") return null;
    const view = window.visualViewport;
    const left = view ? view.offsetLeft : 0, top = view ? view.offsetTop : 0;
    const width = view ? view.width : innerWidth, height = view ? view.height : innerHeight;
    const candidates = new Map();
    const roles = new Set(["button", "link", "tab", "menuitem", "menuitemcheckbox",
                           "menuitemradio", "checkbox", "radio", "switch", "option", "textbox"]);
    const toggles = new Set(["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"]);
    for (const el of document.querySelectorAll("*")) {
        const r = el.getBoundingClientRect();
        const box = [Math.max(r.left, left), Math.max(r.top, top),
                     Math.min(r.right, left + width), Math.min(r.bottom, top + height)];
        if (box[2] - box[0] < 1 || box[3] - box[1] < 1) continue;
        const ariaRole = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
        const editable = el.matches("textarea,input:not([type]),input[type=text],input[type=search]," +
                                    "input[type=email],input[type=url],input[type=tel],input[type=number]," +
                                    "input[type=password],[contenteditable=true]");
        const input = editable && !el.readOnly && el.getAttribute("aria-readonly") !== "true";
        const semantic = editable || roles.has(ariaRole) || el.matches("a[href],button,input,select,summary");
        const hasClick = el.hasAttribute("onclick") || typeof el.onclick === "function" ||
            (typeof getEventListeners === "function" && Boolean(getEventListeners(el).click?.length));
        if (!semantic && !hasClick) continue;
        if (el.matches(":disabled,input[type=hidden]") || el.closest("[inert],[aria-disabled=true]")) continue;
        const style = getComputedStyle(el);
        if (style.visibility !== "visible" || style.pointerEvents === "none" || Number(style.opacity) === 0) continue;
        const role = editable ? "input" : el.matches("a[href]") ? "link" :
            (toggles.has(ariaRole) || el.matches("input[type=checkbox],input[type=radio]")) ? "toggle" :
            (roles.has(ariaRole) ? ariaRole : "button");
        candidates.set(el, { box, role, input, semantic });
    }
    const result = [];
    const nearestCandidate = (el) => {
        for (let node = el; node; node = node.parentElement) if (candidates.has(node)) return node;
        return null;
    };
    const text = (value) => (value || "").replace(/\s+/g, " ").trim().slice(0, 160);
    for (const [el, candidate] of candidates) {
        const [x1, y1, x2, y2] = candidate.box;
        const midX = (x1 + x2) / 2, midY = (y1 + y2) / 2;
        const points = [[midX, midY], [(x1 + midX) / 2, midY], [(midX + x2) / 2, midY],
                        [midX, (y1 + midY) / 2], [midX, (midY + y2) / 2]];
        for (const child of Array.from(el.querySelectorAll("img,svg,span,strong,a")).slice(0, 16)) {
            const rectangle = child.getBoundingClientRect();
            points.push([(rectangle.left + rectangle.right) / 2, (rectangle.top + rectangle.bottom) / 2]);
        }
        const hits = points.filter(([x, y]) => x1 <= x && x < x2 && y1 <= y && y < y2 &&
            nearestCandidate(document.elementFromPoint(x, y)) === el);
        if (!hits.length) continue;
        const referenced = (el.getAttribute("aria-labelledby") || "").split(/\s+/)
            .map(id => document.getElementById(id)?.innerText || "").join(" ");
        const label = el.labels && Array.from(el.labels).map(row => row.innerText).join(" ");
        const name = text(el.getAttribute("aria-label")) || text(referenced) || text(label) ||
            text(el.isContentEditable ? "" : el.innerText) || text(el.getAttribute("alt")) ||
            text(el.getAttribute("placeholder")) || text(el.getAttribute("title"));
        const ancestor = nearestCandidate(el.parentElement);
        if (!name && !candidate.semantic && ancestor && ancestor.matches("button,a[href],[role=button]")) continue;
        const state = {};
        for (const key of ["checked", "selected", "expanded", "pressed"]) {
            const value = el.getAttribute("aria-" + key);
            if (value === "true" || value === "false") state[key] = value === "true";
        }
        if (el.matches("input[type=password]")) state.password = true;
        if (el.matches("input[type=checkbox],input[type=radio]")) state.checked = el.checked;
        result.push({ bounds: [x1 - left, y1 - top, x2 - left, y2 - top],
                      points: hits.map(([x, y]) => [x - left, y - top]), name: name || null,
                      role: candidate.role, actions: candidate.input ? ["tap", "input"] : ["tap"], state });
        if (result.length >= 512) break;
    }
    return { width, height, scale: devicePixelRatio * (view ? view.scale : 1), targets: result };
})()"""


@dataclass(frozen=True)
class WebViewPage:
    bounds: tuple[int, int, int, int]
    width: float
    height: float
    scale: float
    targets: list[dict]
    png: bytes


def _debug_services(device, package, deadline):
    output = observation_io.read_exec(device, _PROCESS_SOCKETS, deadline, 524288).decode(errors="replace")
    process = re.compile(r"\s*(\d+)\s+" + re.escape(package) + r"(?::\S+)?\s*")
    pids = {match[1] for line in output.splitlines() if (match := process.fullmatch(line))}
    sockets = re.findall(r"@(webview_devtools_remote_(\d+))$", output, re.M)
    return sorted({name for name, pid in sockets if pid in pids})


def _targets(device, service, deadline):
    with observation_io.channel(device, service, deadline) as sock, closing(HTTPConnection("localhost")) as http:
        http.sock = sock
        http.request("GET", "/json/list")
        response = http.getresponse()
        if response.status != 200:
            return []
        rows = json.loads(response.read(262145))
        return rows if isinstance(rows, list) else []


def _visible_view(target):
    if not isinstance(target, dict) or target.get("type") != "page":
        return None
    description = json.loads(target.get("description") or "{}")
    if (not isinstance(description, dict) or description.get("visible") is not True
            or description.get("attached") is not True or description.get("empty") is not False):
        return None
    position = [description.get(key) for key in ("screenX", "screenY", "width", "height")]
    if any(type(value) is not int for value in position) or min(position[2:]) <= 0:
        return None
    left, top, width, height = position
    endpoint = urlsplit(target.get("webSocketDebuggerUrl") or "").path
    if not _ENDPOINT.fullmatch(endpoint):
        return None
    return endpoint, (left, top, left + width, top + height)


def _call(ws, sequence, method, params, deadline):
    ws.send(json.dumps({"id": sequence, "method": method, "params": params}))
    while True:
        event = json.loads(ws.recv(timeout=observation_io.remaining(deadline)))
        if event.get("id") == sequence:
            return event.get("result") or {}


def _coordinates(value, count):
    return (isinstance(value, list) and len(value) == count
            and all(type(n) in (int, float) and math.isfinite(n) for n in value))


def _validated_targets(rows, width, height):
    targets = []
    if not isinstance(rows, list):
        return targets
    for row in rows[:512]:
        if not isinstance(row, dict):
            continue
        bounds, points = row.get("bounds"), row.get("points")
        name, role, actions, state = (row.get(key) for key in ("name", "role", "actions", "state"))
        if (not _coordinates(bounds, 4) or not isinstance(points, list)
                or not 0 <= bounds[0] < bounds[2] <= width
                or not 0 <= bounds[1] < bounds[3] <= height
                or (name is not None and not isinstance(name, str))
                or not isinstance(role, str) or actions not in (["tap"], ["tap", "input"])
                or not isinstance(state, dict)):
            continue
        hits = [point for point in points[:21] if _coordinates(point, 2)
                and bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3]]
        if not hits:
            continue
        targets.append({
            "bounds": bounds, "points": hits, "name": name[:160] if name else None,
            "role": role, "actions": actions,
            "state": {key: state[key] for key in _STATE_KEYS if type(state.get(key)) is bool},
        })
    return targets


def _read_page(device, service, endpoint, bounds, deadline):
    with observation_io.channel(device, service, deadline) as sock, connect(
        "ws://localhost" + endpoint, sock=sock, compression=None, max_size=_MAX_MESSAGE,
        open_timeout=observation_io.remaining(deadline), close_timeout=0.05,
    ) as ws:
        response = _call(ws, 1, "Runtime.evaluate", {
            "expression": _READ_ACTIONS, "returnByValue": True, "includeCommandLineAPI": True,
            "silent": True,
        }, deadline)
        value = response.get("result", {}).get("value")
        if not isinstance(value, dict) or not value.get("targets"):
            return None
        width, height, scale = (value[key] for key in ("width", "height", "scale"))
        if not all(type(n) in (int, float) and math.isfinite(n) and n > 0 for n in (width, height, scale)):
            return None
        actual_width, actual_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        if abs(width * scale - actual_width) > 3 or abs(height * scale - actual_height) > 3:
            return None
        targets = _validated_targets(value.get("targets"), width, height)
        if not targets:
            return None
        image = _call(ws, 2, "Page.captureScreenshot", {"format": "png"}, deadline).get("data")
        if image:
            return WebViewPage(bounds, width, height, scale, targets, base64.b64decode(image, validate=True))
        return None


def loading(device, window_dump, deadline):
    focus = _FOCUS.search(window_dump)
    if not focus:
        return False
    try:
        for name in _debug_services(device, focus[1], deadline):
            service = "localabstract:" + name
            for target in _targets(device, service, deadline):
                visible = _visible_view(target)
                if visible is None:
                    continue
                with observation_io.channel(device, service, deadline) as sock, connect(
                    "ws://localhost" + visible[0], sock=sock, compression=None, max_size=_MAX_MESSAGE,
                    open_timeout=observation_io.remaining(deadline), close_timeout=.05,
                ) as ws:
                    response = _call(ws, 1, "Runtime.evaluate", {
                        "expression": _IS_LOADING, "returnByValue": True, "silent": True,
                    }, deadline)
                    if response.get("result", {}).get("value") is True:
                        return True
    except (AdbError, HTTPException, OSError, EOFError, WebSocketException, ValueError, KeyError, TypeError):
        pass  # A missing debug signal cannot establish whether the app is ready.
    return False


def observe(device, window_dump, deadline):
    """Collect visible page evidence within the same observation deadline."""
    focus = _FOCUS.search(window_dump or "")
    if not focus:
        return [], None
    deadline = min(deadline, time.time() + _BUDGET_SECONDS)
    pages = []
    has_service = False
    has_view = False
    try:
        for name in _debug_services(device, focus[1], deadline):
            has_service = True
            service = "localabstract:" + name
            for target in _targets(device, service, deadline):
                visible = _visible_view(target)
                if visible is None:
                    continue
                has_view = True
                page = _read_page(device, service, *visible, deadline)
                if page is not None:
                    pages.append(page)
    except (AdbError, HTTPException, OSError, EOFError, WebSocketException, ValueError, KeyError, TypeError):
        return pages, "webview_read_unavailable"
    if has_service and not pages:
        return [], "webview_read_unavailable" if has_view else "webview_no_visible_target"
    return pages, None


def _same_pixels(first, second, bounds, tolerance):
    size = (min(32, bounds[2] - bounds[0]), min(24, bounds[3] - bounds[1]))
    a = first.crop(bounds).resize(size, Image.Resampling.BOX)
    b = second.crop(bounds).resize(size, Image.Resampling.BOX)
    red, green, blue = ImageChops.difference(a, b).split()
    histogram = ImageChops.lighter(red, ImageChops.lighter(green, blue)).histogram()
    return sum(histogram[27:]) <= tolerance * size[0] * size[1]


def _intersection(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    return (left, top, right, bottom) if left < right and top < bottom else None


def _projected(page, screenshot, display_size):
    width, height = screenshot.size
    device_width, device_height = display_size
    scale_x, scale_y = width / device_width, height / device_height
    view = tuple(round(value * scale) for value, scale in zip(page.bounds, (scale_x, scale_y) * 2))
    visible = _intersection(view, (0, 0, width, height))
    if visible is None or view[2] - view[0] > 2 * width or view[3] - view[1] > 2 * height:
        return
    try:
        with Image.open(io.BytesIO(page.png)) as raw:
            display_width, display_height = page.bounds[2] - page.bounds[0], page.bounds[3] - page.bounds[1]
            if abs(raw.width - display_width) > 3 or abs(raw.height - display_height) > 3:
                return
            image = raw.convert("RGB").resize((view[2] - view[0], view[3] - view[1]), Image.Resampling.BOX)
    except (OSError, ValueError):
        return
    # The renderer excludes native overlays; check each action's pixels and its actual hit point.
    viewport = screenshot.crop(view)
    viewport_box = (0, 0, *viewport.size)
    if not _same_pixels(viewport, image, viewport_box, .35):
        return
    for record in page.targets:
        box = tuple(round(value * page.scale * scale) for value, scale in zip(record["bounds"], (scale_x, scale_y) * 2))
        left, top = view[:2]
        bounds = _intersection((box[0] + left, box[1] + top, box[2] + left, box[3] + top), visible)
        if bounds is None:
            continue
        box = (bounds[0] - left, bounds[1] - top, bounds[2] - left, bounds[3] - top)
        if not _same_pixels(viewport, image, box, .15):
            continue
        for px, py in record["points"]:
            x = round(px * page.scale * scale_x)
            y = round(py * page.scale * scale_y)
            if not box[0] <= x < box[2] or not box[1] <= y < box[3]:
                continue
            hit = _intersection(box, (x - 16, y - 16, x + 17, y + 17))
            # A uniform background can agree even when a native bar covers the WebView.
            detail = [max(high - low for low, high in capture.crop(hit).getextrema())
                      for capture in (viewport, image)]
            if min(detail) < 28 or not _same_pixels(viewport, image, hit, .08):
                continue
            target = {key: value for key, value in record.items() if key != "points"}
            yield {**target, "bounds": list(bounds), "point": [x + left, y + top],
                   "source": "webview", "name_source": "webview" if record["name"] else None}
            break


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _same_control(first, second):
    intersection = _intersection(first["bounds"], second["bounds"])
    if intersection is None:
        return False
    covered = _area(intersection)
    if covered >= .8 * max(_area(first["bounds"]), _area(second["bounds"])):
        return True
    first_name, second_name = first.get("name"), second.get("name")
    smallest = min(_area(first["bounds"]), _area(second["bounds"]))
    if first_name:
        return first_name == second_name and covered >= .75 * smallest
    a, b = first["bounds"], second["bounds"]
    point = second["point"]
    # The Android tree can see a clipped slice of the same unlabelled control.
    return (not second_name and a[0] <= point[0] < a[2] and a[1] <= point[1] < a[3]
            and covered == smallest and smallest >= .5 * max(_area(a), _area(b))
            and ((a[0], a[2]) == (b[0], b[2]) or (a[1], a[3]) == (b[1], b[3])))


def merge(pages, screenshot, display_size, targets, excluded, panel, max_targets):
    """Keep Android controls and add only DOM controls confirmed on this PNG."""
    next_id = 1
    for page in pages:
        for proposal in _projected(page, screenshot, display_size):
            box = proposal["bounds"]
            if panel and tuple(box) != _intersection(box, panel):
                continue
            if any(_intersection(box, obstacle) for obstacle in excluded):
                continue
            matches = [target for target in targets if _same_control(target, proposal)]
            matching = next((target for target in matches if proposal["name"] and target.get("name") == proposal["name"]),
                            matches[0] if matches else None)
            if matching is not None:
                if matching["source"] == "pixels":
                    matching.update(proposal, name_bounds=proposal["bounds"] if proposal["name"] else None)
                elif not matching.get("name") and proposal["name"]:
                    matching.update(name=proposal["name"], name_source="webview", name_bounds=proposal["bounds"])
                continue
            if len(targets) >= max_targets:
                return True
            target = {"id": f"W{next_id}", **proposal}
            targets.append(target)
            next_id += 1
    return False
