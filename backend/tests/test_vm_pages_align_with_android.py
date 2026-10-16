"""三端页面必须消费同一份 Server 生命周期事实，不再分别推导状态和动作。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_PAGES = Path(__file__).resolve().parents[2] / "web" / "src" / "pages"
_ANDROID = _PAGES / "VirtualMachines.vue"
_HARMONY = _PAGES / "HarmonyVirtualMachines.vue"
_IOS_SIM = _PAGES / "IosSimVirtualMachines.vue"
_ALL_PAGES = [_ANDROID, _HARMONY, _IOS_SIM]


@pytest.mark.parametrize("page", _ALL_PAGES)
def test_pages_consume_shared_authoritative_lifecycle(page: Path):
    """禁止复制旧 raw-state/cleanup 推断，避免仅统一文案却保留相互矛盾的动作。"""
    src = page.read_text(encoding="utf-8")
    assert "from '../lib/vmLifecycle.js'" in src
    for old_rule in (
        "function stateLabel(", "function stateClass(", "function canStart(",
        "function canStop(", "function canDelete(", "function canProbe(",
        "hasUnconfirmedRuntime", "statusDetails", "cleanup_confirmed", "cleanup_pending", "vm.state",
    ):
        assert old_rule not in src, f"{page.name} 仍自行判断生命周期：{old_rule}"
    assert ':class="stateClass(vm)">{{ stateLabel(vm) }}' in src


@pytest.mark.parametrize("page", _ALL_PAGES)
def test_normal_actions_and_progress_share_one_rendering_contract(page: Path):
    src = page.read_text(encoding="utf-8")
    assert 'v-if="showStart(vm)"' in src
    assert ':disabled="!canStart(vm) || isVmBusy(vm)"' in src
    assert "lifecycleState(vm) === 'starting' ? '启动中…' : '启动'" in src
    assert 'v-if="showStop(vm)"' in src
    assert ':disabled="!canStop(vm) || isVmBusy(vm)"' in src
    assert "lifecycleState(vm) === 'stopping' ? '停止中…' : '停止'" in src
    assert ':disabled="isVmBusy(vm) || !canCopy(vm)"' in src
    for alternate_action in ("重试停止", "重试启动", "确认停止", "待恢复"):
        assert alternate_action not in src


@pytest.mark.parametrize("page", _ALL_PAGES)
def test_lifecycle_result_and_failure_are_visible(page: Path):
    """错误独立展示，不再被离线状态隐藏，也不提供后台清理流程。"""
    src = page.read_text(encoding="utf-8")
    for obsolete_flow in ("pending_cleanup", "待清理", "重试清理"):
        assert obsolete_flow not in src
    assert 'v-if="lifecycleNotice(vm)"' in src
    assert 'v-if="operationError(vm)"' in src


@pytest.mark.parametrize("page", [_HARMONY, _IOS_SIM], ids=["harmony", "ios_sim"])
def test_meta_fields_are_laid_out_horizontally(page: Path):
    """保留原卡片布局，不因统一状态而把字段改成两行。"""
    src = page.read_text(encoding="utf-8")
    meta_rules = [line for line in src.splitlines() if line.strip().startswith(".meta div")]
    assert meta_rules, f"{page.name} 找不到 .meta div 样式"
    for rule in meta_rules:
        assert "flex-direction: column" not in rule


def test_vm_page_lifecycle_behavior():
    """运行页面及共享helper：权威状态/动作、缺契约、错误快照、并发与渐进探查。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend behavior checks")
    scripts = sorted((_PAGES.parents[1] / "tests").glob("*.test.mjs"))
    assert scripts, "缺少前端行为测试"
    result = subprocess.run(
        [node, "--test", *map(str, scripts)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
