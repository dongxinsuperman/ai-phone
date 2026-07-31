"""Normalize one official DevEco catalog snapshot for Server storage."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any, Dict, Iterator, Optional


CREATABLE_DEVICE_TYPES = (
    "Phone",
    "Foldable",
    "WideFold",
    "TripleFold",
    "Tablet",
    "2in1",
    "2in1 Foldable",
    "Wearable",
    "TV",
)

DEVICE_TYPE_ALIASES = {
    "phone": "Phone",
    "foldable": "Foldable",
    "widefold": "WideFold",
    "triplefold": "TripleFold",
    "tablet": "Tablet",
    "2in1": "2in1",
    "2in1 foldable": "2in1 Foldable",
    "wearable": "Wearable",
    "tv": "TV",
    # DevEco 6.1.0.410 的官方 -imageList 会返回这一类镜像，但同版本
    # -create -help 并未把它列为可创建的 deviceType。目录必须完整保留，
    # 前后端则明确标为不可创建，不能静默漏掉或冒充普通 Wearable。
    "wearablekid": "WearableKid",
}
DEVICE_TYPES = (*CREATABLE_DEVICE_TYPES, "WearableKid")
BUNDLED_CATALOG_RESOURCE = "official_catalog.json"
BUNDLED_COMPAT_RESOURCE = "create_compat.json"
COMPAT_BASIS = "emulator_create_probe"


def load_bundled_manifest() -> Dict[str, Any]:
    """Load the checked-in DevEco export used to initialize an empty Server DB."""
    resource = files("ai_phone.server.harmony_vm").joinpath(
        BUNDLED_CATALOG_RESOURCE
    )
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("bundled_harmony_catalog_must_be_object")
    return manifest


@lru_cache(maxsize=1)
def load_create_compat() -> Dict[str, Dict[str, Any]]:
    """Load the probed ``Emulator -create`` compatibility table.

    Produced by ``scripts/probe_harmony_catalog.py``. It records, per
    ``deviceType + osVersion``, whether the combination can be created at all,
    whether that version accepts ``-screenProfile``, and exactly which models
    the CLI resolves. Only covers creation — never treat it as proof that the
    instance boots.
    """
    resource = files("ai_phone.server.harmony_vm").joinpath(
        BUNDLED_COMPAT_RESOURCE
    )
    payload = json.loads(resource.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ValueError("bundled_harmony_compat_entries_missing")
    return {
        str(key): value
        for key, value in entries.items()
        if isinstance(value, dict)
    }


def _compat_entry(device_type: str, os_version: str) -> Optional[Dict[str, Any]]:
    # 不吞异常。内置兼容表缺失或损坏时，正确行为是让鸿蒙目录初始化明确失败、
    # 鸿蒙能力显式不可用（方案 4.0），而不是降级成"所有机型都未确认"——那样
    # 接口仍然回一份看着正常、实则一台机型都选不出来的空目录，把故障藏起来。
    return load_create_compat().get(f"{device_type}|{os_version}")


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
            return parsed
        except json.JSONDecodeError:
            continue
    return value


def _objects(
    value: Any, inherited: Optional[Dict[str, Any]] = None
) -> Iterator[Dict[str, Any]]:
    context = dict(inherited or {})
    if isinstance(value, dict):
        context.update(
            {
                key: child
                for key, child in value.items()
                if not isinstance(child, (dict, list))
            }
        )
        yield {**context, **value}
        for child in value.values():
            yield from _objects(child, context)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child, context)


def _pick(row: Dict[str, Any], *names: str) -> Any:
    lowered = {
        str(key).lower().replace("_", ""): value for key, value in row.items()
    }
    for name in names:
        value = lowered.get(name.lower().replace("_", ""))
        if value not in (None, ""):
            return value
    return ""


def _device_type(row: Dict[str, Any], raw: str = "") -> str:
    value = str(
        _pick(row, "deviceType", "device_type", "deviceCategory", "type") or ""
    ).strip()
    normalized = DEVICE_TYPE_ALIASES.get(value.lower())
    if normalized:
        return normalized
    for alias in sorted(DEVICE_TYPE_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", raw, re.IGNORECASE):
            return DEVICE_TYPE_ALIASES[alias]
    return ""


def _version_spec(row: Dict[str, Any]) -> str:
    value = str(
        _pick(
            row,
            "osVersion",
            "os_version",
            "versionName",
            "softwareVersion",
            "version",
        )
        or ""
    ).strip()
    if re.fullmatch(
        r"HarmonyOS\s+\d+(?:\.\d+)*(?:\(\d+\))(?:\s+Beta\d+)?", value
    ):
        return value
    api = str(_pick(row, "apiVersion", "api", "apiLevel") or "").strip()
    if value and api:
        return f"HarmonyOS {value}({api})"
    return ""


def _apply_create_compat(item: Dict[str, Any]) -> None:
    """Overlay the probed creation verdict onto one image entry.

    ``CREATABLE_DEVICE_TYPES`` only knows which device types ``-create``
    accepts; it cannot tell that HarmonyOS 5.x refuses ``-screenProfile``
    entirely, so the probe result wins wherever it exists. Combinations the
    probe never saw stay explicitly unknown rather than being guessed either
    way — front and back end must show them as unverified.
    """
    compat = _compat_entry(
        str(item.get("device_type") or ""), str(item.get("os_version") or "")
    )
    if compat is None:
        item["compat_status"] = "unknown"
        item["custom_screen_supported"] = None
        item["default_screen"] = {}
        return
    default_screen = compat.get("default_screen") or {}
    custom_screen = compat.get("custom_screen") or {}
    creatable = bool(compat.get("creatable"))
    item["compat_status"] = "probed"
    item["creatable"] = creatable
    item["custom_screen_supported"] = custom_screen.get("supported")
    item["default_screen"] = default_screen.get("config") or {}
    # unavailable_reason 是给调用方判断用的稳定枚举，CLI 原文只作为证据放在
    # 另一个字段，避免 Emulator 改一句提示就让接口语义跟着变。
    item["unavailable_reason"] = (
        "" if creatable else "not_supported_by_emulator_create"
    )
    item["unavailable_detail"] = (
        "" if creatable else str(default_screen.get("reason") or "")
    )


def _model_key(name: str) -> frozenset[str]:
    """官方机型名的可比较形式。

    同一台机器在两处官方数据里的写法会不一致——机型列表写
    ``nova 15 Ultra、nova 15 Pro``，实例配置写 ``nova 15 Pro、nova 15 Ultra``。
    分组内顺序不同但指同一组机器，所以按名字集合比较。
    """
    return frozenset(
        part.strip() for part in re.split(r"[、,/]", name) if part.strip()
    )


def _default_model_name(compat: Dict[str, Any]) -> str:
    config = (compat.get("default_screen") or {}).get("config") or {}
    return str(config.get("productModel") or "").strip()


def _creatable_profile_methods(
    device_type: str, os_version: str
) -> Dict[frozenset[str], str]:
    """机型 -> 该组合下用哪种官方方式创建。

    ``screen_profile`` 是把机型名交给 ``-screenProfile``；``default`` 是这一版
    Emulator 不接受该机型名，但它本来就是该形态的默认机型，两个参数都不传就会
    建出它。HarmonyOS 5.x 全部走后者——机型不可选，但并非未知。

    不提供第三种“按屏幕参数还原”的方式：那样建出来的实例
    ``productModel`` 会变成 ``Customize__*``，等于用假身份冒充官方机型。
    """
    compat = _compat_entry(device_type, os_version)
    if compat is None or not compat.get("creatable"):
        return {}
    methods: Dict[frozenset[str], str] = {}
    default_name = _default_model_name(compat)
    if default_name:
        methods[_model_key(default_name)] = "default"
    custom_screen = compat.get("custom_screen") or {}
    if custom_screen.get("supported") is True:
        names = custom_screen.get("profiles_creatable")
        if isinstance(names, list):
            methods.update({_model_key(str(name)): "screen_profile" for name in names})
    return methods


def normalize_images(value: Any) -> list[Dict[str, Any]]:
    parsed = _json_value(value)
    found: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for row in _objects(parsed):
        version = _version_spec(row)
        raw = json.dumps(row, ensure_ascii=False, default=str)
        device_type = _device_type(row, raw)
        if not version or not device_type:
            continue
        api = re.search(r"\((\d+)\)", version)
        abi = str(_pick(row, "abi", "cpuArch", "architecture") or "auto").strip()
        if abi == "arm64-v8a":
            abi = "arm64"
        creatable = device_type in CREATABLE_DEVICE_TYPES
        item = {
            "id": f"{device_type}|{version}|{abi or 'auto'}",
            "device_type": device_type,
            "device_type_cli": str(
                _pick(row, "device_type_cli", "deviceType") or device_type
            ),
            "os_version": version,
            "api_version": api.group(1) if api else "",
            "abi": abi or "auto",
            "software_version": str(
                _pick(row, "SoftWareVersion", "softwareVersion") or ""
            ).strip(),
            "release_type": str(_pick(row, "releaseType") or "").strip(),
            "upgradable": str(_pick(row, "upgradable") or "").lower()
            in {"true", "1", "yes"},
            "creatable": creatable,
            "unavailable_reason": (
                "" if creatable else "not_supported_by_emulator_create"
            ),
        }
        found[(device_type, version, abi or "auto")] = item
    if not found and isinstance(parsed, str):
        current_type = ""
        for line in parsed.splitlines():
            detected = _device_type({}, line)
            if detected:
                current_type = detected
            for version in re.findall(
                r"HarmonyOS\s+\d+(?:\.\d+)*(?:\(\d+\))(?:\s+Beta\d+)?",
                line,
            ):
                device_type = detected or current_type
                if not device_type:
                    continue
                api = re.search(r"\((\d+)\)", version)
                found[(device_type, version, "auto")] = {
                    "id": f"{device_type}|{version}|auto",
                    "device_type": device_type,
                    "device_type_cli": device_type,
                    "os_version": version,
                    "api_version": api.group(1) if api else "",
                    "abi": "auto",
                    "software_version": "",
                    "release_type": "",
                    "upgradable": False,
                    "creatable": device_type in CREATABLE_DEVICE_TYPES,
                    "unavailable_reason": (
                        ""
                        if device_type in CREATABLE_DEVICE_TYPES
                        else "not_supported_by_emulator_create"
                    ),
                }
    for item in found.values():
        _apply_create_compat(item)
    return sorted(
        found.values(), key=lambda item: (item["device_type"], item["os_version"])
    )


def normalize_screen_profiles(value: Any) -> list[Dict[str, Any]]:
    parsed = _json_value(value)
    found: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in _objects(parsed):
        name = str(_pick(row, "model", "name", "profileName") or "").strip()
        raw = json.dumps(row, ensure_ascii=False, default=str)
        device_type = _device_type(row, raw)
        if not name or not device_type:
            continue
        item: Dict[str, Any] = {
            "id": f"{device_type}|{name}",
            "device_type": device_type,
            "name": name,
        }
        values = (
            ("width", _pick(row, "width", "screenWidth", "resolutionWidth")),
            ("height", _pick(row, "height", "screenHeight", "resolutionHeight")),
            ("density", _pick(row, "dpi", "density")),
            (
                "size_in",
                _pick(row, "size_in", "size", "screenSize", "diagonal"),
            ),
            (
                "outer_width",
                _pick(row, "outer_width", "outerScreenWidth"),
            ),
            (
                "outer_height",
                _pick(row, "outer_height", "outerScreenHeight"),
            ),
            (
                "outer_size_in",
                _pick(row, "outer_size_in", "outerScreenSize", "outerDiagonal"),
            ),
        )
        for key, field_value in values:
            if field_value not in ("", None):
                item[key] = field_value
        screen = re.search(
            r'"(?:screen|screenConfig)"\s*:\s*"'
            r"(\d{3,5})[\s,]+(\d{3,5})[\s,]+"
            r"(\d{2,4})[\s,]+(\d+(?:\.\d+)?)",
            raw,
            re.IGNORECASE,
        )
        if screen:
            item.setdefault("width", int(screen.group(1)))
            item.setdefault("height", int(screen.group(2)))
            item.setdefault("density", int(screen.group(3)))
            item.setdefault("size_in", screen.group(4))
        found[(device_type, name)] = item
    if not found and isinstance(parsed, str):
        current_type = ""
        current: Optional[Dict[str, Any]] = None
        for line in parsed.splitlines():
            header = re.match(
                r"^\s*(?:(?P<type>2in1 Foldable|2in1|Foldable|Phone|"
                r"Tablet|WideFold|TripleFold|Wearable|TV)\s+)?-\s+"
                r'["\']?(?P<name>.+?)["\']?\s*$',
                line,
                re.IGNORECASE,
            )
            if header:
                detected = _device_type(
                    {"deviceType": header.group("type") or current_type}
                )
                if detected:
                    current_type = detected
                name = header.group("name").strip()
                if not current_type:
                    current = None
                    continue
                current = {
                    "id": f"{current_type}|{name}",
                    "device_type": current_type,
                    "name": name,
                }
                found[(current_type, name)] = current
                continue
            if current is None:
                continue
            outer_resolution = re.search(
                r"\bouter\s+screen\s*:\s*(\d{3,5})\s*[*xX×]\s*(\d{3,5})",
                line,
                re.IGNORECASE,
            )
            resolution = re.search(
                r"^\s*screen\s*:\s*(\d{3,5})\s*[*xX×]\s*(\d{3,5})",
                line,
                re.IGNORECASE,
            )
            outer_size = re.search(
                r"\bouter\s+diagonal\s*:\s*(\d+(?:\.\d+)?)",
                line,
                re.IGNORECASE,
            )
            size = re.search(
                r"^\s*diagonal\s*:\s*(\d+(?:\.\d+)?)",
                line,
                re.IGNORECASE,
            )
            density = re.search(
                r"^\s*density\s*:\s*(\d{2,4})", line, re.IGNORECASE
            )
            if outer_resolution:
                current["outer_width"] = int(outer_resolution.group(1))
                current["outer_height"] = int(outer_resolution.group(2))
            elif resolution:
                current["width"] = int(resolution.group(1))
                current["height"] = int(resolution.group(2))
            if outer_size:
                current["outer_size_in"] = outer_size.group(1)
            elif size:
                current["size_in"] = size.group(1)
            if density:
                current["density"] = int(density.group(1))
    return sorted(
        found.values(), key=lambda item: (item["device_type"], item["name"])
    )


def normalize_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    images_source = (
        manifest.get("images")
        or manifest.get("image_list")
        or manifest.get("imageList")
        or []
    )
    profiles_source = (
        manifest.get("screen_profiles")
        or manifest.get("screen_profile_list")
        or manifest.get("screenProfileList")
        or []
    )
    images = normalize_images(images_source)
    profiles = normalize_screen_profiles(profiles_source)
    if not images:
        raise ValueError("official_images_empty")
    if not profiles:
        raise ValueError("official_screen_profiles_empty")
    # DevEco 的官方数据本身分成两份且没有连线：
    # - imageList: deviceType + osVersion
    # - screenProfileList/productConfig.json: deviceType + 具体机型
    # 按 deviceType 做笛卡尔积会拼出大量被 CLI 拒绝的组合（同一份机型列表里，
    # HarmonyOS 5.x 一个机型都不接受，6.x 也只接受其中一部分）。消费者侧的
    # “支持机型”名单同样对不上——它描述真机能升到哪个版本，而不是模拟器实现了
    # 哪些机型模板。因此这里以 scripts/probe_harmony_catalog.py 的 -create 实测
    # 结果为准；未实测到的组合留空而不是放开，绝不根据机型上市时间猜版本。
    for profile in profiles:
        device_type = str(profile["device_type"])
        key = _model_key(str(profile["name"]))
        supported: list[str] = []
        methods: Dict[str, str] = {}
        for image in images:
            if str(image["device_type"]) != device_type:
                continue
            if image.get("creatable", True) is False:
                continue
            method = _creatable_profile_methods(
                device_type, str(image["os_version"])
            ).get(key)
            if not method:
                continue
            image_id = str(image["id"])
            supported.append(image_id)
            # 同一机型在不同版本上的创建方式可能不同：默认机型在 5.x 是唯一选项，
            # 到 6.0.x 又能按名字选。所以按镜像逐条记录，不能压成一个值。
            methods[image_id] = method
        profile["supported_image_ids"] = supported
        profile["create_methods"] = methods
        profile["image_compatibility_basis"] = COMPAT_BASIS
    device_types = sorted(
        {item["device_type"] for item in [*images, *profiles]}
    )
    return {
        "device_types": device_types,
        "images": images,
        "screen_profiles": profiles,
        "emulator_version": str(manifest.get("emulator_version") or "").strip(),
    }


__all__ = [
    "BUNDLED_CATALOG_RESOURCE",
    "BUNDLED_COMPAT_RESOURCE",
    "COMPAT_BASIS",
    "CREATABLE_DEVICE_TYPES",
    "DEVICE_TYPES",
    "load_bundled_manifest",
    "load_create_compat",
    "normalize_images",
    "normalize_manifest",
    "normalize_screen_profiles",
]
