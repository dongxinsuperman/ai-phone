"""三端虚拟机页面的用户可见口径必须一致，以 Android 为基准。

为什么需要这个检查：措辞和渲染这类东西没有编译期约束，也不会有单测覆盖，
全靠写的人记得去比对——而实际发生过的是，iOS 页面照着当时的鸿蒙抄，把
``agent_offline`` 显示成「Agent 离线」并铺一行红字；鸿蒙后来改对了，iOS 却
留在原地。等用户发现时，三端已经各说各话。

基准是 **Android**（``VirtualMachines.vue``）：它是虚拟机生命周期的样板，
另外两端只在技术上有差异的地方才允许不同。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_PAGES = Path(__file__).resolve().parents[2] / "web" / "src" / "pages"
_ANDROID = _PAGES / "VirtualMachines.vue"
_HARMONY = _PAGES / "HarmonyVirtualMachines.vue"
_IOS_SIM = _PAGES / "IosSimVirtualMachines.vue"


def _state_labels(path: Path) -> dict:
    """抽出 stateLabel 里的 状态 → 中文 映射。"""
    src = path.read_text(encoding="utf-8")
    body = src.split("function stateLabel(")[1].split("}[state]")[0]
    return dict(re.findall(r"(\w+):\s*'([^']+)'", body))


# Android 独有的状态：它的下发是两段式（先 dispatching 再 starting），
# 鸿蒙与 iOS 都是一步到位，没有这个中间态。
_ANDROID_ONLY = {"dispatching"}


@pytest.mark.parametrize(
    "page", [_HARMONY, _IOS_SIM], ids=["harmony", "ios_sim"]
)
def test_state_labels_match_android(page: Path):
    """同一个状态在三端必须是同一个词。

    用户不该因为「换了个平台」就要重新学一遍词汇，尤其 agent_offline——
    它是等 Agent 重连认领的**正常中间态**，叫「Agent 离线」像是出了故障。
    """
    android = _state_labels(_ANDROID)
    other = _state_labels(page)

    shared = (set(android) - _ANDROID_ONLY) & set(other)
    assert shared, "没抽到共有状态，说明解析逻辑失效了"

    mismatched = {
        state: (android[state], other[state])
        for state in sorted(shared)
        if android[state] != other[state]
    }
    assert not mismatched, (
        f"{page.name} 的状态措辞与 Android 不一致 "
        f"（状态: (Android, 本页)）：{mismatched}"
    )


@pytest.mark.parametrize(
    "page", [_HARMONY, _IOS_SIM], ids=["harmony", "ios_sim"]
)
def test_recoverable_state_is_not_rendered_as_error(page: Path):
    """``agent_offline`` 不能渲染成红色错误行。

    它会自动恢复（Agent 重连即认领回来），后端把原因写进 error_message 只为
    留痕。Android 压根不展示这个字段；另外两端要展示的话，至少必须把这个
    状态排除掉。
    """
    src = page.read_text(encoding="utf-8")
    if "error_message" not in src:
        pytest.skip("本页不展示 error_message，与 Android 完全一致")

    for match in re.finditer(r'v-if="vm\.error_message([^"]*)"', src):
        condition = match.group(1)
        assert "agent_offline" in condition, (
            f"{page.name} 展示 error_message 时没有排除 agent_offline，"
            "会把可自动恢复的中间态显示成故障"
        )


@pytest.mark.parametrize(
    "page", [_HARMONY, _IOS_SIM], ids=["harmony", "ios_sim"]
)
def test_meta_fields_are_laid_out_horizontally(page: Path):
    """卡片字段必须「名 + 值」同一行，与 Android 一致。

    竖排（``flex-direction: column``）会让每个字段占两行，卡片高度直接翻倍，
    一屏放不下几台。这是纯样式问题，不会报错、不会被任何单测覆盖，只能这样钉住。
    """
    src = page.read_text(encoding="utf-8")
    meta_rules = [
        line for line in src.splitlines() if line.strip().startswith(".meta div")
    ]
    assert meta_rules, f"{page.name} 里找不到 .meta div 样式，解析逻辑可能失效"
    for rule in meta_rules:
        assert "flex-direction: column" not in rule, (
            f"{page.name} 的卡片字段是竖排的，会让卡片高度翻倍：{rule.strip()}"
        )


def test_android_does_not_render_error_message():
    """基准自身的行为：Android 卡片上只有状态徽章，不铺红字。

    这条钉住基准。哪天 Android 改了口径，上面两个检查的前提就变了，
    应该由这条先红，提醒人重新对齐，而不是让另外两端悄悄跑偏。
    """
    assert "vm.error_message" not in _ANDROID.read_text(encoding="utf-8")
