"""The public frame and typed controls shared by AI Connect device adapters."""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import replace
from typing import Annotated, Literal, Union

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_phone.agent.android_frame_marks import annotate_frame
from ai_phone.shared.android_frame_regions import read_sample

from . import unified
from .frame_fields import TARGET_FIELDS, TEXT_FIELDS, public_keyboard, public_record, public_structure


Identifier = Annotated[str, Field(min_length=1, max_length=64)]
Pixel = Annotated[int, Field(strict=True, ge=0)]
ExpectedText = Annotated[str, Field(min_length=1, max_length=160)]


class DeviceAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_id: Identifier | None = None
    expected_text: ExpectedText | None = None


class Tap(DeviceAction):
    action: Literal["tap"]
    frame_id: Identifier
    target: Identifier | None = None
    x: Pixel | None = None
    y: Pixel | None = None

    @model_validator(mode="after")
    def choose_position(self):
        if self.target is not None:
            if self.x is not None or self.y is not None:
                raise ValueError("按编号点击时只传 target")
        elif self.x is None or self.y is None:
            raise ValueError("请给出 target 或一对原图坐标 x、y")
        return self


class Scroll(DeviceAction):
    action: Literal["scroll"]
    frame_id: Identifier
    direction: Literal["up", "down", "left", "right"]
    target: Identifier | None = None


class Input(DeviceAction):
    action: Literal["input"]
    text: str = Field(max_length=8192)


class OpenApp(DeviceAction):
    action: Literal["open_app"]
    # iOS permits hyphens and Harmony permits underscores. The selected adapter validates its own bundle identifier.
    package: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class Navigation(DeviceAction):
    action: Literal["back", "home"]


Action = Annotated[Union[Tap, Scroll, Input, OpenApp, Navigation], Field(discriminator="action")]


def _frame_target_ids(targets: list, width: int, height: int) -> set[str]:
    ids = set()
    for target in targets:
        ident = target.get("id") if isinstance(target, dict) else None
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("invalid or duplicated frame-local target ID")
        actions = target.get("actions")
        if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
            raise ValueError("invalid target actions")
        point = target.get("point")
        if point is not None and (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(type(v) is int for v in point) or not (0 <= point[0] < width and 0 <= point[1] < height)):
            raise ValueError("invalid target point in this screenshot")
        box = target.get("bounds")
        if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(type(v) is int for v in box):
            raise ValueError("invalid target rectangle in this screenshot")
        left, top, right, bottom = max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])
        if left >= right or top >= bottom:
            raise ValueError("target rectangle has no visible pixels")
        target["bounds"] = [left, top, right, bottom]
        ids.add(ident)
    return ids


async def observe(hub, lease, *, expected_text: str | None = None) -> dict:
    # A refresh consumes the old action numbers even if the Agent fails to supply the replacement frame.
    lease.screen = None
    platform = lease.unified_target.platform
    options = {"wait_ready": True} if platform == "android" else {"include_screenshot": True}
    if expected_text:
        if platform != "android":
            raise HTTPException(422, "预期文字目前仅支持 Android")
        options["expected_text"] = expected_text
    result = await unified.operate(hub, lease, "observe", **options)
    image, screen = unified.read_screen(result)
    png = result["png"]
    try:
        required = ("texts", "targets", "keyboard", "dialogs", "warnings", "tree_available", "texts_truncated")
        fields = {key: result[key] for key in required}
        if (not isinstance(fields["texts"], list) or not all(isinstance(v, str) for v in fields["texts"])
                or not isinstance(fields["targets"], list) or not isinstance(fields["warnings"], list)
                or not all(isinstance(v, str) for v in fields["warnings"])
                or type(fields["tree_available"]) is not bool or type(fields["texts_truncated"]) is not bool):
            raise ValueError("bad observation")
        fields["targets"] = [public_record(row, TARGET_FIELDS) for row in fields["targets"]]
        ids = _frame_target_ids(fields["targets"], screen.width, screen.height)
        if fields["keyboard"] is not None:
            fields["keyboard"] = public_keyboard(fields["keyboard"], screen.width, screen.height)
        if fields["dialogs"] is not None:
            if not isinstance(fields["dialogs"], list):
                raise ValueError("invalid dialog evidence")
            fields["dialogs"] = [public_record(row, ("kind", "label", "role", "name", "bounds", "source"))
                                 for row in fields["dialogs"]]
        marked = result.get("marked_targets", False)
        if type(marked) is not bool:
            raise ValueError("invalid screenshot marking flag")
        if fields["targets"] and not marked:
            # Legacy observers supply the original PNG. Draw precisely the public list on that image.
            image = await asyncio.to_thread(annotate_frame, image, fields["targets"])
            png = base64.b64encode(image).decode("ascii")
            marked = True
        for key in ("system_texts", "screen_texts"):
            if key in result:
                records = result[key]
                if not isinstance(records, list) or not all(isinstance(entry, dict) for entry in records):
                    raise ValueError("invalid text evidence list")
                if any(target is not None and (not isinstance(target, str) or target not in ids)
                       for target in (entry.get("target") for entry in records)):
                    raise ValueError("text refers to a target outside this frame")
                fields[key] = [public_record(row, TEXT_FIELDS) for row in records]
        fields.update(width=screen.width, height=screen.height, platform=platform, marked_targets=marked)
        for key in ("targets_truncated", "system_texts_truncated", "screen_texts_truncated", "screen_texts_available"):
            if key in result:
                if type(result[key]) is not bool:
                    raise ValueError("invalid evidence availability flag")
                fields[key] = result[key]
        fields.update(public_record(result, ("package", "activity", "rotation")))
        if result.get("structure_status") in ("available", "partial", "unavailable"):
            fields["structure_status"] = result["structure_status"]
        if result.get("readiness") in ("expected_text_present", "no_loading_signal"):
            fields["readiness"] = result["readiness"]
        if expected_text and fields.get("readiness") != "expected_text_present":
            raise HTTPException(503, "当前 Agent 未确认预期文字")
        fields["structure"] = public_structure(result, fields, ids)
    except (KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
        raise HTTPException(502, "设备未返回完整的画面信息") from exc
    # Only the lease sees the unmarked image sample. The public PNG contains the original-resolution IDs.
    snapshot = fields.copy()
    raw_grid = read_sample(result.get("_raw_grid_v1"), screen.width, screen.height)
    if raw_grid is not None:
        snapshot["_raw_grid_v1"] = raw_grid
    lease.screen = replace(screen, observation=snapshot)
    return {"frame_id": screen.ident, **fields,
            "screenshot": {"mime_type": "image/png", "data": png, "annotated": marked, "marked_targets": marked}}


def find_target(screen: unified.Screen, ident: str) -> dict:
    if screen.observation is None:
        raise HTTPException(409, "请先用 observe 读取当前画面的目标编号")
    for target in screen.observation["targets"]:
        if target["id"] == ident:
            return target
    raise HTTPException(422, "这张画面中没有该目标编号")


def parameters(body: Action, screen: unified.Screen | None, platform: str = "android") -> dict:
    if body.frame_id is not None and (screen is None or screen.ident != body.frame_id):
        raise HTTPException(409, "画面编号已失效，请用 observe 获取当前画面")
    if isinstance(body, (Tap, Scroll)):
        # These actions always have a validated frame_id.
        assert screen is not None
        params = {"width": screen.width, "height": screen.height, "display": screen.display}
        target = find_target(screen, body.target) if body.target is not None else None
        if isinstance(body, Tap):
            if target is not None:
                if not target.get("point") or not {"tap", "try_tap"}.intersection(target["actions"]):
                    raise HTTPException(422, "该目标没有可用的点击位置")
                x, y = target["point"]
            else:
                x, y = body.x, body.y
            if not (0 <= x < screen.width and 0 <= y < screen.height):
                raise HTTPException(422, "点击坐标超出截图范围")
            return {**params, "x": x, "y": y}
        if target is not None:
            if "scroll" not in target["actions"]:
                raise HTTPException(422, "该目标没有声明滚动动作")
            params["bounds"] = target["bounds"]
        return {**params, "direction": body.direction}
    if isinstance(body, Input):
        return {"text": body.text}
    if isinstance(body, OpenApp):
        if platform == "android" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*", body.package):
            raise HTTPException(422, "Android 包名无效")
        return {"package": body.package}
    return {}
