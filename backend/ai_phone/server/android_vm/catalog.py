"""Android VM device catalog helpers.

Real device profiles come from imported official sources. Built-in entries in
this file are either source-backed curated device presets or coverage
strategies. Coverage strategies must not be presented as real devices.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def builtin_coverage_profiles() -> list[Dict[str, Any]]:
    return [
        _coverage(
            "android5-legacy-720p",
            "Android 5 / 720p（极老系统兼容）",
            "覆盖 Android 5 时代的低分辨率、低内存旧机档案；是否可启动取决于 Agent 是否安装 API 21 system image。",
            ["system_version", "android5", "legacy", "low_end"],
            api_level=21,
            width=720,
            height=1280,
            density=320,
            ram_mb=1024,
            cpu_cores=2,
            vm_heap_mb=128,
            storage_mb=4096,
            system_type="default",
        ),
        _coverage(
            "android6-legacy-720p",
            "Android 6 / 720p（旧系统兼容）",
            "覆盖 Android 6 运行时、老系统 WebView/权限/兼容行为；是否可启动取决于 Agent 是否安装 API 23 system image。",
            ["system_version", "android6", "legacy", "low_end"],
            api_level=23,
            width=720,
            height=1280,
            density=320,
            ram_mb=1536,
            cpu_cores=2,
            vm_heap_mb=128,
            storage_mb=4096,
            system_type="default",
        ),
        _coverage(
            "android7-legacy-720p",
            "Android 7 / 720p（旧系统稳定性）",
            "覆盖 Android 7 时代的旧机档案，适合回归老权限、老 WebView 和低内存行为。",
            ["system_version", "android7", "legacy", "low_end"],
            api_level=24,
            width=720,
            height=1280,
            density=320,
            ram_mb=2048,
            cpu_cores=2,
            vm_heap_mb=192,
            storage_mb=4096,
            system_type="default",
        ),
        _coverage(
            "android8-legacy-1080p",
            "Android 8 / 1080p（旧主流兼容）",
            "覆盖 Android 8 权限、通知、WebView 与老设备兼容问题；是否可启动取决于 Agent 镜像安装情况。",
            ["system_version", "android8", "legacy", "mainstream"],
            api_level=26,
            width=1080,
            height=1920,
            density=420,
            ram_mb=3072,
            cpu_cores=4,
            vm_heap_mb=256,
            storage_mb=8192,
            system_type="default",
        ),
        _coverage(
            "android9-cutout-era-1080p",
            "Android 9 / 1080p（异形屏时代）",
            "覆盖 Android 9 开始的 display cutout 与主流 1080p 设备档案。",
            ["system_version", "screen_shape", "android9", "cutout", "mainstream"],
            api_level=28,
            width=1080,
            height=2244,
            density=420,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
            display_extra={"cutout": "notch", "shape_note": "Android 9+ 可模拟 display cutout，用于安全区布局测试"},
        ),
        _coverage(
            "low-720p-android10",
            "低端 720p / Android 10（老系统低配覆盖）",
            "覆盖低分辨率、低内存、老系统兼容问题，不代表某台真实设备。",
            ["system_version", "low_end", "old_android", "android10", "layout"],
            api_level=29,
            width=720,
            height=1600,
            density=320,
            ram_mb=2048,
            cpu_cores=2,
            vm_heap_mb=192,
            storage_mb=4096,
        ),
        _coverage(
            "android11-mainstream-1080p",
            "Android 11 / 主流 1080p（存量兼容）",
            "覆盖 Android 11 存量主流设备档案。",
            ["system_version", "android11", "mainstream", "regression"],
            api_level=30,
            width=1080,
            height=2400,
            density=420,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
        ),
        _coverage(
            "android12-mainstream-1080p",
            "Android 12 / 主流 1080p（系统兼容）",
            "覆盖 Android 12 权限、通知、启动与主流屏幕兼容问题。",
            ["system_version", "android12", "mainstream", "regression"],
            api_level=31,
            width=1080,
            height=2400,
            density=420,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
        ),
        _coverage(
            "mainstream-1080p-android13",
            "主流 1080p / Android 13（存量主流覆盖）",
            "覆盖主流手机屏幕、内存和 Android 13 兼容性。",
            ["system_version", "mainstream", "android13", "regression"],
            api_level=33,
            width=1080,
            height=2400,
            density=420,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
        ),
        _coverage(
            "android14-mainstream-1080p",
            "Android 14 / 主流 1080p（新主流兼容）",
            "覆盖 Android 14 权限、通知、后台限制和主流屏幕档案。",
            ["system_version", "android14", "mainstream", "regression"],
            api_level=34,
            width=1080,
            height=2400,
            density=420,
            ram_mb=6144,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=12288,
        ),
        _coverage(
            "high-dpi-flagship-android15",
            "高 DPI 旗舰 / Android 15（高密度布局覆盖）",
            "覆盖高 DPI、小屏旗舰类 UI 缩放问题，不宣称等价某个厂商 ROM。",
            ["screen_shape", "system_version", "high_dpi", "flagship", "android15"],
            api_level=35,
            width=1200,
            height=2670,
            density=460,
            ram_mb=8192,
            cpu_cores=6,
            vm_heap_mb=512,
            storage_mb=16384,
        ),
        _coverage(
            "android16-modern-flagship",
            "Android 16 / 高性能档（新系统预研）",
            "覆盖 Android 16 新系统预研和现代高性能屏幕档案；是否可启动取决于 Agent 镜像安装情况。",
            ["system_version", "android16", "modern", "flagship", "preflight"],
            api_level=36,
            width=1200,
            height=2670,
            density=460,
            ram_mb=8192,
            cpu_cores=6,
            vm_heap_mb=512,
            storage_mb=16384,
        ),
        _coverage(
            "tablet-android12l",
            "平板大屏 / Android 12L（大屏布局覆盖）",
            "覆盖平板和大屏布局，适合检查响应式、分栏、横竖屏。",
            ["screen_shape", "tablet", "large_screen", "android12l"],
            api_level=32,
            width=1600,
            height=2560,
            density=320,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
        ),
        _coverage(
            "cutout-hole-android14",
            "挖孔屏 / Android 14（异形屏近似）",
            "覆盖挖孔屏/安全区/高屏占比布局风险；Android 9+ 可通过开发者选项模拟 cutout，厂商真实挖孔裁剪仍需真机验证。",
            ["screen_shape", "cutout", "hole", "android14", "safe_area"],
            api_level=34,
            width=1080,
            height=2400,
            density=440,
            ram_mb=6144,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
            display_extra={"cutout": "hole", "shape_note": "近似挖孔屏安全区，不等价真实厂商状态栏裁剪"},
        ),
        _coverage(
            "notch-android12",
            "刘海屏 / Android 12（异形屏近似）",
            "覆盖刘海屏、状态栏安全区和全屏页面布局风险；真实厂商策略仍需真机验证。",
            ["screen_shape", "cutout", "notch", "android12", "safe_area"],
            api_level=31,
            width=1080,
            height=2280,
            density=420,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
            display_extra={"cutout": "notch", "shape_note": "近似刘海屏安全区，不等价真实厂商状态栏裁剪"},
        ),
        _coverage(
            "small-screen-android11",
            "小屏 / Android 11（窄屏布局覆盖）",
            "覆盖小屏、低宽度、按钮换行和列表密度问题。",
            ["screen_shape", "small_screen", "android11", "layout"],
            api_level=30,
            width=720,
            height=1520,
            density=360,
            ram_mb=3072,
            cpu_cores=3,
            vm_heap_mb=256,
            storage_mb=8192,
        ),
        _coverage(
            "weak-network-cold-boot",
            "弱网冷启动（网络/启动稳定性覆盖）",
            "覆盖低速高延迟网络、冷启动和清数据场景。",
            ["scenario", "network", "cold_boot", "stability"],
            api_level=34,
            width=1080,
            height=2400,
            density=420,
            ram_mb=4096,
            cpu_cores=4,
            vm_heap_mb=384,
            storage_mb=8192,
            network_speed="gsm",
            network_delay="edge",
            wipe_data=True,
            snapshot_policy="cold_boot",
        ),
    ]


def builtin_device_profiles() -> list[Dict[str, Any]]:
    """设备库不再内置手写预设：一律来自官方 CSV 导入。空库即空，不兜底假数据。"""
    return []


def _legacy_builtin_device_profiles() -> list[Dict[str, Any]]:
    """[已停用] 历史手写预设，任何接口都不再调用；仅保留作参考，可在清理提交中删除。"""
    return [
        _device(
            "preset-redmi-k70",
            manufacturer="Xiaomi",
            brand="Redmi",
            marketing_name="Redmi K70",
            model_code="K70",
            device="redmi_k70",
            source_url="https://www.mi.com/redmi-k70/specs",
            screen_size_in="6.67",
            screen_width=1440,
            screen_height=3200,
            density=526,
            ram_mb=8192,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Snapdragon 8 Gen 2 class",
            gpu="Adreno class",
            tags=["xiaomi", "redmi", "k70", "high_dpi", "flagship"],
            diff_note="近似屏幕、DPI、Android 版本和性能档；不复刻 HyperOS、真实 SoC/GPU 性能和厂商权限弹窗。",
            series="Redmi K",
            popularity_score=92,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android14", "android15", "punch_hole", "high_dpi", "flagship"],
        ),
        _device(
            "preset-xiaomi-14",
            manufacturer="Xiaomi",
            brand="Xiaomi",
            marketing_name="Xiaomi 14",
            model_code="Xiaomi 14",
            device="xiaomi_14",
            source_url="https://www.mi.com/global/product/xiaomi-14/specs",
            screen_size_in="6.36",
            screen_width=1200,
            screen_height=2670,
            density=460,
            ram_mb=8192,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Snapdragon 8 Gen 3 class",
            gpu="Adreno class",
            tags=["xiaomi", "flagship", "small_flagship", "high_dpi"],
            diff_note="近似小屏旗舰的屏幕和性能档；不复刻 HyperOS、Leica 相机栈和厂商系统行为。",
            series="Xiaomi 数字",
            popularity_score=90,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android14", "android15", "punch_hole", "small_screen", "high_dpi", "flagship"],
        ),
        _device(
            "preset-pixel-8",
            manufacturer="Google",
            brand="Google",
            marketing_name="Pixel 8",
            model_code="Pixel 8",
            device="shiba",
            source_url="https://support.google.com/pixelphone/answer/7158570",
            screen_size_in="6.2",
            screen_width=1080,
            screen_height=2400,
            density=428,
            ram_mb=8192,
            sdk_versions=["34", "35", "36"],
            abis=["arm64-v8a"],
            soc="Google Tensor G3 class",
            gpu="Mali class",
            tags=["google", "pixel", "reference", "mainstream"],
            diff_note="近似 Pixel 8 屏幕、Android 版本和内存档；真实 Pixel 系统特性仍需真机验证。",
            series="Pixel",
            market_region="GLOBAL",
            popularity_source="reference_device",
            popularity_score=18,
            screen_shape="punch_hole",
            market_tags=["reference", "android14", "android15", "android16", "punch_hole"],
        ),
        _device(
            "preset-galaxy-s24",
            manufacturer="Samsung",
            brand="Samsung",
            marketing_name="Galaxy S24",
            model_code="Galaxy S24",
            device="galaxy_s24",
            source_url="https://www.samsung.com/global/galaxy/galaxy-s24/specs/",
            screen_size_in="6.2",
            screen_width=1080,
            screen_height=2340,
            density=416,
            ram_mb=8192,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Snapdragon/Exynos flagship class",
            gpu="flagship class",
            tags=["samsung", "galaxy", "oneui", "flagship"],
            diff_note="近似 Galaxy S24 屏幕和内存档；不复刻 One UI、厂商权限和真实芯片差异。",
            series="Galaxy S",
            market_region="GLOBAL",
            popularity_source="reference_device",
            popularity_score=24,
            screen_shape="punch_hole",
            market_tags=["reference", "android14", "android15", "punch_hole", "flagship"],
        ),
        _device(
            "preset-galaxy-s24-ultra",
            manufacturer="Samsung",
            brand="Samsung",
            marketing_name="Galaxy S24 Ultra",
            model_code="Galaxy S24 Ultra",
            device="galaxy_s24_ultra",
            source_url="https://www.samsung.com/global/galaxy/galaxy-s24-ultra/specs/",
            screen_size_in="6.8",
            screen_width=1440,
            screen_height=3120,
            density=505,
            ram_mb=12288,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Snapdragon 8 Gen 3 class",
            gpu="Adreno class",
            tags=["samsung", "galaxy", "oneui", "flagship", "large_screen"],
            diff_note="近似大屏旗舰屏幕、DPI 和内存档；不复刻 One UI、S Pen、厂商相机和真实芯片差异。",
            series="Galaxy S",
            market_region="GLOBAL",
            popularity_source="reference_device",
            popularity_score=22,
            screen_shape="punch_hole",
            market_tags=["reference", "android14", "android15", "punch_hole", "large_screen", "high_dpi", "flagship"],
        ),
        _device(
            "preset-pixel-8-pro",
            manufacturer="Google",
            brand="Google",
            marketing_name="Pixel 8 Pro",
            model_code="Pixel 8 Pro",
            device="husky",
            source_url="https://support.google.com/pixelphone/answer/7158570",
            screen_size_in="6.7",
            screen_width=1344,
            screen_height=2992,
            density=489,
            ram_mb=12288,
            sdk_versions=["34", "35", "36"],
            abis=["arm64-v8a"],
            soc="Google Tensor G3 class",
            gpu="Mali class",
            tags=["google", "pixel", "reference", "flagship", "large_screen"],
            diff_note="近似 Pixel 8 Pro 屏幕、Android 版本和内存档；真实 Pixel 系统特性仍需真机验证。",
            series="Pixel",
            market_region="GLOBAL",
            popularity_source="reference_device",
            popularity_score=16,
            screen_shape="punch_hole",
            market_tags=["reference", "android14", "android15", "android16", "punch_hole", "large_screen", "high_dpi"],
        ),
        _device(
            "preset-pixel-7a",
            manufacturer="Google",
            brand="Google",
            marketing_name="Pixel 7a",
            model_code="Pixel 7a",
            device="lynx",
            source_url="https://support.google.com/pixelphone/answer/7158570",
            screen_size_in="6.1",
            screen_width=1080,
            screen_height=2400,
            density=429,
            ram_mb=8192,
            sdk_versions=["33", "34", "35"],
            abis=["arm64-v8a"],
            soc="Google Tensor G2 class",
            gpu="Mali class",
            tags=["google", "pixel", "reference", "mid_range", "small_screen"],
            diff_note="近似 Pixel 7a 中端小屏档案；真实 Pixel 系统特性仍需真机验证。",
            series="Pixel",
            market_region="GLOBAL",
            popularity_source="reference_device",
            popularity_score=14,
            screen_shape="punch_hole",
            market_tags=["reference", "android13", "android14", "android15", "punch_hole", "small_screen"],
        ),
        _device(
            "preset-oneplus-12",
            manufacturer="OnePlus",
            brand="OnePlus",
            marketing_name="OnePlus 12",
            model_code="OnePlus 12",
            device="oneplus_12",
            source_url="https://www.oneplus.com/12/specs",
            screen_size_in="6.82",
            screen_width=1440,
            screen_height=3168,
            density=510,
            ram_mb=12288,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Snapdragon 8 Gen 3 class",
            gpu="Adreno class",
            tags=["oneplus", "flagship", "large_screen", "high_dpi"],
            diff_note="近似 OnePlus 12 大屏旗舰档案；不复刻 OxygenOS/ColorOS、相机和厂商权限行为。",
            series="一加数字",
            popularity_score=82,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android14", "android15", "punch_hole", "large_screen", "high_dpi", "flagship"],
        ),
        _device(
            "preset-oppo-find-x7",
            manufacturer="OPPO",
            brand="OPPO",
            marketing_name="OPPO Find X7",
            model_code="Find X7",
            device="oppo_find_x7",
            source_url="https://www.oppo.com/cn/smartphones/series-find-x/find-x7/specs/",
            screen_size_in="6.78",
            screen_width=1264,
            screen_height=2780,
            density=450,
            ram_mb=12288,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Dimensity flagship class",
            gpu="flagship class",
            tags=["oppo", "find", "coloros", "flagship", "large_screen"],
            diff_note="近似 OPPO Find X7 屏幕和性能档；不复刻 ColorOS、厂商权限弹窗和真实芯片性能。",
            series="OPPO Find",
            popularity_score=84,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android14", "android15", "punch_hole", "large_screen", "high_dpi", "flagship"],
        ),
        _device(
            "preset-vivo-x100",
            manufacturer="vivo",
            brand="vivo",
            marketing_name="vivo X100",
            model_code="X100",
            device="vivo_x100",
            source_url="https://www.vivo.com.cn/vivo/param/x100",
            screen_size_in="6.78",
            screen_width=1260,
            screen_height=2800,
            density=453,
            ram_mb=12288,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="Dimensity flagship class",
            gpu="flagship class",
            tags=["vivo", "x100", "originos", "flagship", "large_screen"],
            diff_note="近似 vivo X100 屏幕和性能档；不复刻 OriginOS、厂商权限弹窗和相机栈。",
            series="vivo X",
            popularity_score=86,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android14", "android15", "punch_hole", "large_screen", "high_dpi", "flagship"],
        ),
        _device(
            "preset-redmi-note-13-pro-plus-5g",
            manufacturer="Xiaomi",
            brand="Redmi",
            marketing_name="Redmi Note 13 Pro+ 5G",
            model_code="Redmi Note 13 Pro+ 5G",
            device="redmi_note_13_pro_plus_5g",
            source_url="https://www.mi.com/global/product/redmi-note-13-pro-plus-5g/specs",
            screen_size_in="6.67",
            screen_width=1220,
            screen_height=2712,
            density=446,
            ram_mb=8192,
            sdk_versions=["33", "34", "35"],
            abis=["arm64-v8a"],
            soc="Dimensity mid-high class",
            gpu="Mali class",
            tags=["xiaomi", "redmi", "mid_range", "high_dpi"],
            diff_note="近似红米中高端曲面屏档案；不复刻 MIUI/HyperOS、厂商权限和真实芯片性能。",
            series="Redmi Note",
            popularity_score=88,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android13", "android14", "android15", "punch_hole", "mid_range", "high_dpi"],
        ),
        _device(
            "preset-honor-90",
            manufacturer="HONOR",
            brand="HONOR",
            marketing_name="HONOR 90",
            model_code="HONOR 90",
            device="honor_90",
            source_url="https://www.honor.com/global/phones/honor-90/specs/",
            screen_size_in="6.7",
            screen_width=1200,
            screen_height=2664,
            density=435,
            ram_mb=8192,
            sdk_versions=["33", "34"],
            abis=["arm64-v8a"],
            soc="Snapdragon 7 series class",
            gpu="Adreno class",
            tags=["honor", "mid_range", "high_dpi"],
            diff_note="近似 HONOR 90 屏幕和中端性能档；不复刻 MagicOS、厂商权限和真实芯片性能。",
            series="荣耀数字",
            popularity_score=76,
            screen_shape="punch_hole",
            market_tags=["cn_common", "android13", "android14", "punch_hole", "mid_range", "high_dpi"],
        ),
        _device(
            "preset-huawei-p30",
            manufacturer="HUAWEI",
            brand="HUAWEI",
            marketing_name="HUAWEI P30",
            model_code="P30",
            device="huawei_p30",
            source_url="https://consumer.huawei.com/cn/phones/p30/specs/",
            screen_size_in="6.1",
            screen_width=1080,
            screen_height=2340,
            density=422,
            ram_mb=8192,
            sdk_versions=["28", "29"],
            abis=["arm64-v8a"],
            soc="Kirin 980 class",
            gpu="Mali class",
            tags=["huawei", "p30", "legacy_android", "waterdrop"],
            diff_note="近似 HUAWEI P30 屏幕、DPI、Android 9/10 时代配置；不复刻 EMUI/HarmonyOS 和厂商权限行为。",
            series="HUAWEI P",
            popularity_score=70,
            screen_shape="waterdrop",
            market_tags=["cn_common", "android9", "android10", "waterdrop", "legacy_system", "compact"],
        ),
        _device(
            "preset-redmi-9a",
            manufacturer="Xiaomi",
            brand="Redmi",
            marketing_name="Redmi 9A",
            model_code="Redmi 9A",
            device="redmi_9a",
            source_url="https://www.mi.com/global/redmi-9a/specs",
            screen_size_in="6.53",
            screen_width=720,
            screen_height=1600,
            density=269,
            ram_mb=2048,
            sdk_versions=["29", "30"],
            abis=["arm64-v8a"],
            soc="Helio G25 class",
            gpu="PowerVR class",
            tags=["xiaomi", "redmi", "entry", "waterdrop", "low_resolution"],
            diff_note="近似 Redmi 9A 低端 720p、水滴屏和 Android 10/11 时代配置；不复刻 MIUI 厂商行为。",
            series="Redmi 数字",
            popularity_score=74,
            screen_shape="waterdrop",
            market_tags=["cn_common", "android10", "android11", "waterdrop", "low_end", "low_resolution"],
        ),
        _device(
            "preset-redmi-4a",
            manufacturer="Xiaomi",
            brand="Redmi",
            marketing_name="Redmi 4A",
            model_code="Redmi 4A",
            device="redmi_4a",
            source_url="https://www.mi.com/in/redmi-4a/specs/",
            screen_size_in="5.0",
            screen_width=720,
            screen_height=1280,
            density=296,
            ram_mb=2048,
            sdk_versions=["23", "24"],
            abis=["arm64-v8a"],
            soc="Snapdragon 425 class",
            gpu="Adreno class",
            tags=["xiaomi", "redmi", "legacy_android", "classic_16_9", "low_resolution"],
            diff_note="近似 Redmi 4A 的 Android 6/7、720p、小屏低端配置；是否可启动取决于 Agent 是否安装对应 API system image。",
            series="Redmi 数字",
            popularity_score=58,
            screen_shape="classic_16_9",
            market_tags=["coverage_gap", "android6", "android7", "small_screen", "low_end", "low_resolution"],
        ),
        _device(
            "preset-huawei-p8",
            manufacturer="HUAWEI",
            brand="HUAWEI",
            marketing_name="HUAWEI P8",
            model_code="P8",
            device="huawei_p8",
            source_url="https://consumer.huawei.com/en/phones/p8/specs/",
            screen_size_in="5.2",
            screen_width=1080,
            screen_height=1920,
            density=424,
            ram_mb=3072,
            sdk_versions=["21", "22"],
            abis=["arm64-v8a"],
            soc="Kirin 930 class",
            gpu="Mali class",
            tags=["huawei", "p8", "legacy_android", "classic_16_9"],
            diff_note="近似 HUAWEI P8 的 Android 5 时代小屏配置；不复刻 EMUI 和旧厂商系统行为。",
            series="HUAWEI P",
            popularity_score=52,
            screen_shape="classic_16_9",
            market_tags=["coverage_gap", "android5", "small_screen", "legacy_system"],
        ),
        _device(
            "preset-galaxy-a15-5g",
            manufacturer="Samsung",
            brand="Samsung",
            marketing_name="Galaxy A15 5G",
            model_code="Galaxy A15 5G",
            device="galaxy_a15_5g",
            source_url="https://www.samsung.com/global/galaxy/galaxy-a15-5g/specs/",
            screen_size_in="6.5",
            screen_width=1080,
            screen_height=2340,
            density=399,
            ram_mb=4096,
            sdk_versions=["34", "35"],
            abis=["arm64-v8a"],
            soc="entry 5G class",
            gpu="entry class",
            tags=["samsung", "galaxy", "entry", "low_mid_range"],
            diff_note="近似入门 5G 机型的 1080p、低内存档案；不复刻 One UI、厂商权限和真实芯片性能。",
            series="Galaxy A",
            market_region="GLOBAL",
            popularity_source="reference_device",
            popularity_score=18,
            screen_shape="waterdrop",
            market_tags=["reference", "android14", "android15", "waterdrop", "low_mid_range"],
        ),
    ]


def parse_play_device_catalog_csv(
    csv_text: str,
    *,
    source_url: str = "",
    collected_at: Optional[datetime] = None,
) -> list[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[Dict[str, Any]] = []
    collected = collected_at or datetime.now(timezone.utc)
    for raw in reader:
        normalized = {_norm_key(k): (v or "").strip() for k, v in raw.items() if k}
        manufacturer = _pick(normalized, "manufacturer", "oem", "brand_owner")
        marketing_name = _pick(normalized, "model_name", "marketing_name", "name")
        model_code = _pick(normalized, "model_code", "model", "model_id")
        brand = _pick(normalized, "brand", "retail_brand") or manufacturer
        device = _pick(normalized, "device", "device_name", "product")
        if not any((manufacturer, brand, device, model_code, marketing_name)):
            continue
        width, height = _parse_resolution(
            _pick(normalized, "screen_sizes", "screen_size", "screen_resolution", "resolution")
        )
        rows.append({
            "source_type": "google_play_device_catalog",
            "source_url": source_url,
            "collected_at": collected,
            "confidence": "official",
            "verification_status": "verified",
            "popularity_source": "imported_official_catalog",
            "popularity_score": 0,
            "market_region": "",
            "manufacturer": manufacturer,
            "brand": brand,
            "series": "",
            "device": device,
            "model_code": model_code,
            "marketing_name": marketing_name,
            "variant_key": _variant_key(normalized),
            "form_factor": _pick(normalized, "form_factor", "device_type"),
            "screen_shape": "",
            "market_tags": [],
            "ram_mb": _parse_mb(_pick(normalized, "ram", "ram_totalmem", "ram_total_memory", "total_memory")),
            "soc": _pick(normalized, "system_on_chip", "soc"),
            "gpu": _pick(normalized, "gpu"),
            # screen_size_in 语义是「屏幕英寸」；官方目录无真实英寸数据，
            # 早期误把分辨率串（Screen Sizes）塞这里会显示成 "1440x3200in"。留空更诚实，
            # 真实分辨率已由 screen_width/height 承载，原始串仍保留在 raw 里。
            "screen_size_in": "",
            "screen_width": width,
            "screen_height": height,
            "densities": _split_values(_pick(normalized, "screen_densities", "screen_density", "densities")),
            "abis": _split_values(_pick(normalized, "abis", "abi")),
            "sdk_versions": _split_values(_pick(normalized, "android_sdk_versions", "sdk_versions", "android_versions")),
            "opengl_es": _pick(normalized, "opengl_es_version", "opengl_es"),
            "raw": raw,
        })
    return rows


def parse_google_supported_devices_csv(
    csv_text: str,
    *,
    source_url: str = "https://storage.googleapis.com/play_public/supported_devices.csv",
    collected_at: Optional[datetime] = None,
) -> list[Dict[str, Any]]:
    """Parse Google's public supported devices identity CSV.

    This public file only contains identity columns. It confirms that a device
    identity exists, but it does not carry enough screen/API data to become a
    user-selectable VM preset by itself, so rows are imported as
    candidate_pending.
    """
    text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[Dict[str, Any]] = []
    collected = collected_at or datetime.now(timezone.utc)
    for raw in reader:
        normalized = {_norm_key(k): (v or "").strip() for k, v in raw.items() if k}
        brand = _pick(normalized, "retail_branding", "retail_brand", "brand")
        marketing_name = _pick(normalized, "marketing_name", "model_name", "name")
        device = _pick(normalized, "device", "device_name")
        model_code = _pick(normalized, "model", "model_code")
        if not any((brand, marketing_name, device, model_code)):
            continue
        rows.append({
            "source_type": "google_play_public_supported_devices",
            "source_url": source_url,
            "collected_at": collected,
            "confidence": "official_identity",
            "verification_status": "candidate_pending",
            "popularity_source": "google_public_identity_catalog",
            "popularity_score": 0,
            "market_region": "",
            "manufacturer": brand,
            "brand": brand,
            "series": "",
            "device": device,
            "model_code": model_code,
            "marketing_name": marketing_name,
            "variant_key": _identity_variant_key(brand, device, model_code, marketing_name),
            "form_factor": "",
            "screen_shape": "",
            "market_tags": ["identity_only"],
            "ram_mb": None,
            "soc": "",
            "gpu": "",
            "screen_size_in": "",
            "screen_width": None,
            "screen_height": None,
            "densities": [],
            "abis": [],
            "sdk_versions": [],
            "opengl_es": "",
            "raw": raw | {
                "source_note": (
                    "Google public supported_devices only confirms device "
                    "identity; specs must be verified before selection."
                )
            },
        })
    return rows


# ---------------------------------------------------------------------------
# 预清洗（纯本地，不联网）：Form Factor 过滤 / 品牌归一化 / 分辨率档 / 去重
# ---------------------------------------------------------------------------

# 默认只保留手机 + 平板（手表 / TV / Chromebook / 车机滤掉）。
SUPPORTED_FORM_FACTORS = {"phone", "tablet"}

# 品牌名归一化（CSV 里大小写五花八门，lge 实为 LG）。
_BRAND_DISPLAY = {
    "lge": "LG", "lg": "LG", "samsung": "Samsung", "oppo": "OPPO",
    "vivo": "vivo", "xiaomi": "Xiaomi", "redmi": "Redmi", "huawei": "HUAWEI",
    "honor": "HONOR", "oneplus": "OnePlus", "realme": "realme",
    "google": "Google", "motorola": "Motorola", "sony": "Sony",
    "zte": "ZTE", "tcl": "TCL", "lenovo": "Lenovo", "asus": "ASUS",
    "meizu": "Meizu", "nubia": "nubia", "tecno": "TECNO", "infinix": "Infinix",
    "blackview": "Blackview", "doogee": "DOOGEE", "blu": "BLU",
}


def normalize_brand(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return _BRAND_DISPLAY.get(raw.lower(), raw[:1].upper() + raw[1:])


def is_supported_form_factor(value: str) -> bool:
    return (value or "").strip().lower() in SUPPORTED_FORM_FACTORS


def resolution_bucket(width: Any, height: Any) -> str:
    """按短边把分辨率归档，供"按分辨率"筛选。"""
    try:
        w, h = int(width or 0), int(height or 0)
    except Exception:
        return ""
    if w <= 0 or h <= 0:
        return ""
    short = min(w, h)
    if short <= 540:
        return "qHD-及以下"
    if short <= 720:
        return "720p"
    if short <= 1080:
        return "1080p"
    if short <= 1300:
        return "1.5K"
    if short <= 1600:
        return "2K"
    return "2K+"


# 各字符串列的数据库长度上限（与 models.AndroidDeviceProfile 对齐）。
# CSV 数据很野（多分辨率拼串、超长 SoC/型号名等），入库前按列长度截断，防 22001。
_STR_LIMITS: Dict[str, int] = {
    "source_type": 64, "source_url": 512, "confidence": 32,
    "verification_status": 32, "popularity_source": 128, "market_region": 32,
    "manufacturer": 128, "brand": 128, "series": 128, "device": 128,
    "model_code": 128, "marketing_name": 128, "variant_key": 128,
    "form_factor": 64, "screen_shape": 64, "soc": 128, "gpu": 128,
    "screen_size_in": 128, "opengl_es": 64, "resolution_bucket": 16, "sdk_index": 128,
}


def cap_profile_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """把设备 profile 行的字符串字段按 DB 列长度截断，避免插入超长报错。"""
    for key, limit in _STR_LIMITS.items():
        value = row.get(key)
        if isinstance(value, str) and len(value) > limit:
            row[key] = value[:limit]
    return row


def _abi_sorted(abis: Any) -> list[str]:
    items = [str(a).strip() for a in (abis or []) if str(a).strip()]
    return sorted(items, key=lambda a: (a != "arm64-v8a", a))


def _variant_score(row: Dict[str, Any]) -> tuple[int, int]:
    """同型号多变体取"更高规格"的一条：SDK 越新、RAM 越大越优。"""
    sdks = [int(x) for x in (row.get("sdk_versions") or []) if str(x).isdigit()]
    return (max(sdks, default=0), int(row.get("ram_mb") or 0))


def clean_and_dedupe_profiles(
    rows: Iterable[Dict[str, Any]],
    *,
    allow_form_factors: Optional[set[str]] = None,
) -> list[Dict[str, Any]]:
    """官方目录原始行 → 预清洗：

    1. Form Factor 只留手机 + 平板；
    2. 品牌名归一化；
    3. ABI 排序（arm64-v8a 优先）；
    4. 派生分辨率档写入 raw.resolution_bucket；
    5. 同型号（品牌 + 营销名）多变体去重，保留更高规格的一条。
    """
    allow = allow_form_factors or SUPPORTED_FORM_FACTORS
    best: Dict[tuple, Dict[str, Any]] = {}
    order: list[tuple] = []
    for raw_row in rows:
        ff = (raw_row.get("form_factor") or "").strip().lower()
        if ff not in allow:
            continue
        row = dict(raw_row)
        row["brand"] = normalize_brand(row.get("brand") or row.get("manufacturer") or "")
        row["abis"] = _abi_sorted(row.get("abis"))
        bucket = resolution_bucket(row.get("screen_width"), row.get("screen_height"))
        # 派生列：供服务端按分辨率/系统版本筛选 + 分页
        row["resolution_bucket"] = bucket
        sdks = [str(x).strip() for x in (row.get("sdk_versions") or []) if str(x).strip()]
        row["sdk_index"] = (";" + ";".join(sdks) + ";") if sdks else ""
        meta = dict(row.get("raw") or {})
        meta["resolution_bucket"] = bucket
        row["raw"] = meta
        cap_profile_fields(row)
        name = (row.get("marketing_name") or "").strip().lower()
        if not name:
            name = (row.get("device") or row.get("model_code") or "").strip().lower()
        key = (row["brand"].lower(), name)
        prev = best.get(key)
        if prev is None:
            best[key] = row
            order.append(key)
        elif _variant_score(row) > _variant_score(prev):
            best[key] = row
    return [best[k] for k in order]


def _coverage(
    profile_id: str,
    name: str,
    description: str,
    tags: list[str],
    *,
    api_level: int,
    width: int,
    height: int,
    density: int,
    ram_mb: int,
    cpu_cores: int,
    vm_heap_mb: int,
    storage_mb: int,
    network_speed: str = "full",
    network_delay: str = "none",
    wipe_data: bool = False,
    snapshot_policy: str = "discard_changes",
    system_type: str = "google_apis",
    display_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    display = {
        "screen_width": width,
        "screen_height": height,
        "density": density,
        "orientation": "portrait",
        "screen_size_in": "",
    }
    display.update(display_extra or {})
    return {
        "id": profile_id,
        "name": name,
        "description": description,
        "tags": tags,
        "source_type": "internal_strategy",
        "config_template": {
            "system": {"api_level": api_level, "system_type": system_type, "abi": "auto"},
            "display": display,
            "performance": {
                "ram_mb": ram_mb,
                "cpu_cores": cpu_cores,
                "vm_heap_mb": vm_heap_mb,
                "gpu_mode": "host",
            },
            "storage": {
                "internal_storage_mb": storage_mb,
                "sdcard_mb": 0,
                "wipe_data": wipe_data,
                "snapshot_policy": snapshot_policy,
            },
            "network": {"speed": network_speed, "delay": network_delay, "dns_server": "", "http_proxy": ""},
            "hardware": {
                "back_camera": "emulated",
                "front_camera": "none",
                "gps": True,
                "accelerometer": True,
                "gyroscope": True,
                "proximity": False,
                "hardware_keyboard": False,
                "navigation_style": "none",
            },
            "startup": {
                "no_window": True,
                "no_audio": True,
                "no_boot_anim": True,
                "writable_system": False,
            },
        },
        "capability_marks": _default_capability_marks(),
    }


def _device(
    profile_id: str,
    *,
    manufacturer: str,
    brand: str,
    marketing_name: str,
    model_code: str,
    device: str,
    source_url: str,
    screen_size_in: str,
    screen_width: int,
    screen_height: int,
    density: int,
    ram_mb: int,
    sdk_versions: list[str],
    abis: list[str],
    soc: str,
    gpu: str,
    tags: list[str],
    diff_note: str,
    series: str = "",
    market_region: str = "CN",
    verification_status: str = "verified",
    popularity_source: str = "cn_market_and_verified_specs",
    popularity_score: int = 50,
    screen_shape: str = "",
    market_tags: Optional[list[str]] = None,
    form_factor: str = "PHONE",
) -> Dict[str, Any]:
    combined_tags = list(dict.fromkeys([*(market_tags or []), *tags]))
    raw = {
        "preset_id": profile_id,
        "tags": combined_tags,
        "diff_note": diff_note,
        "managed_by": "ai-phone-internal-catalog",
    }
    return {
        "id": profile_id,
        "source_type": "curated_official_specs",
        "source_url": source_url,
        "collected_at": None,
        "confidence": "official_specs",
        "verification_status": verification_status,
        "popularity_source": popularity_source,
        "popularity_score": popularity_score,
        "market_region": market_region,
        "manufacturer": manufacturer,
        "brand": brand,
        "series": series,
        "device": device,
        "model_code": model_code,
        "marketing_name": marketing_name,
        "variant_key": profile_id,
        "form_factor": form_factor,
        "screen_shape": screen_shape,
        "market_tags": combined_tags,
        "ram_mb": ram_mb,
        "soc": soc,
        "gpu": gpu,
        "screen_size_in": screen_size_in,
        "screen_width": screen_width,
        "screen_height": screen_height,
        "densities": [str(density)],
        "abis": abis,
        "sdk_versions": sdk_versions,
        "opengl_es": "",
        "raw": raw,
        "config_template": {
            "system": {"api_level": max(int(v) for v in sdk_versions), "system_type": "google_apis", "abi": "auto"},
            "display": {
                "screen_width": screen_width,
                "screen_height": screen_height,
                "density": density,
                "orientation": "portrait",
                "screen_size_in": screen_size_in,
                "screen_shape": screen_shape,
            },
            "performance": {
                "ram_mb": ram_mb,
                "cpu_cores": 6 if ram_mb >= 8192 else 4,
                "vm_heap_mb": 512 if ram_mb >= 8192 else 384,
                "gpu_mode": "host",
            },
            "storage": {
                "internal_storage_mb": 16384,
                "sdcard_mb": 0,
                "wipe_data": False,
                "snapshot_policy": "discard_changes",
            },
            "network": {"speed": "full", "delay": "none", "dns_server": "", "http_proxy": ""},
            "hardware": {
                "back_camera": "emulated",
                "front_camera": "none",
                "gps": True,
                "accelerometer": True,
                "gyroscope": True,
                "proximity": False,
                "hardware_keyboard": False,
                "navigation_style": "none",
            },
            "startup": {
                "no_window": True,
                "no_audio": True,
                "no_boot_anim": True,
                "writable_system": False,
            },
            "identity": raw | {
                "source_type": "curated_official_specs",
                "source_url": source_url,
                "confidence": "official_specs",
                "verification_status": verification_status,
                "popularity_source": popularity_source,
                "popularity_score": popularity_score,
                "market_region": market_region,
                "manufacturer": manufacturer,
                "brand": brand,
                "series": series,
                "device": device,
                "model_code": model_code,
                "marketing_name": marketing_name,
                "screen_shape": screen_shape,
                "market_tags": combined_tags,
                "soc": soc,
                "gpu": gpu,
                "abis": abis,
                "sdk_versions": sdk_versions,
            },
        },
        "capability_marks": _default_capability_marks(),
    }


def _default_capability_marks() -> Dict[str, Any]:
    return {
        "system": "avd_profile",
        "display": "avd_profile",
        "performance": "emulator_flag",
        "storage": "emulator_flag",
        "network": "emulator_flag",
        "hardware": "avd_profile",
        "startup": "emulator_flag",
        "identity": "metadata_only",
    }


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def _pick(row: Dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(_norm_key(key), "")
        if value:
            return value
    return ""


def _split_values(value: str) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;/|]+", value) if part.strip()]


def _parse_mb(value: str) -> Optional[int]:
    if not value:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(gb|gib|mb|mib)?", value.strip().lower())
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "mb"
    if unit in {"gb", "gib"}:
        return int(number * 1024)
    return int(number)


def _parse_resolution(value: str) -> tuple[Optional[int], Optional[int]]:
    match = re.search(r"([0-9]{3,5})\s*[x×]\s*([0-9]{3,5})", value or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _variant_key(row: Dict[str, str]) -> str:
    parts = [
        _pick(row, "brand", "retail_brand"),
        _pick(row, "device", "device_name"),
        _pick(row, "model_code", "model"),
        _pick(row, "ram", "ram_total_memory"),
        _pick(row, "android_sdk_versions", "sdk_versions"),
    ]
    return "|".join(part for part in parts if part)[:128]


def _identity_variant_key(
    brand: str,
    device: str,
    model_code: str,
    marketing_name: str,
) -> str:
    return "|".join(
        part for part in (brand, device, model_code, marketing_name) if part
    )[:128]
