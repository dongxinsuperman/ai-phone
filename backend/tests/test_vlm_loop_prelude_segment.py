"""起跑线机制按段感知改造的单元测试。

背景：起跑线（自动 close+open app）原本采用"全文扫"逻辑，结果对于
带『测试标题/前置条件/操作步骤/预期结果』结构化标签的 case 会出两类
问题——

1. 测试标题/预期结果段里出现的 ``「UI 元素名」`` 与前置条件段里的
   ``「真正的 App 名」`` 并列频次时，``Counter.most_common`` 按文本出现
   位置返回，结果抓到 UI 元素名而不是真正的 App 名；
2. 操作步骤段里描述"关闭后重启 App 验证持久化"这种业务步骤，被起跑线
   误判为前置条件偷跑两步，导致后续 VLM 主循环重复执行同一段 close+
   open，业务语义被破坏。

修复后：起跑线对结构化 case 只看『前置条件』段；非结构化 / 自由对话型
goal 退到原"全文扫"兜底，行为完全不变。

注意：本测试中所有 App 名 / UI 文案均使用大众公开应用（淘宝 / 微信 /
抖音）与通用名词（详情 / 按钮），不绑任何特定业务。
"""
from __future__ import annotations

import pytest

from ai_phone.agent.runner.vlm_loop import (
    _detect_app_lifecycle_prelude,
    _split_goal_by_segments,
)


# ---------------------------------------------------------------------------
# _split_goal_by_segments —— 段切分基础能力
# ---------------------------------------------------------------------------
def test_split_goal_by_segments_basic_structured():
    """标准结构化 goal —— 4 段都能正确拆出。"""
    goal = (
        "测试标题:【正向】首页底部展示「详情」入口\n"
        "前置条件：关闭 App「淘宝」（杀进程）后重新打开 App「淘宝」\n"
        "操作步骤：从首页底部 Tab 进入「我的」\n"
        "预期结果：右下角显示蓝色「详情」链接"
    )
    segments = _split_goal_by_segments(goal)
    assert "测试标题" in segments and "详情" in segments["测试标题"]
    assert "前置条件" in segments and "淘宝" in segments["前置条件"]
    assert "操作步骤" in segments and "我的" in segments["操作步骤"]
    assert "预期结果" in segments and "详情" in segments["预期结果"]
    # 段间不串扰：前置条件段不应包含操作步骤段的内容
    assert "我的" not in segments["前置条件"]
    assert "详情" not in segments["前置条件"]


def test_split_goal_by_segments_empty_for_freeform():
    """无任何段头标签的自由对话 goal → 返回空 dict，调用方退到全文扫。"""
    assert _split_goal_by_segments("杀掉「淘宝」重新打开做下单") == {}
    assert _split_goal_by_segments("打开「微信」找联系人") == {}
    assert _split_goal_by_segments("") == {}


# ---------------------------------------------------------------------------
# _detect_app_lifecycle_prelude —— 改造后的起跑线判定
# ---------------------------------------------------------------------------
def test_detect_prelude_real_world_case_picks_app_from_precondition():
    """回归保护：测试标题 / 预期结果各 1 次「UI 元素」、前置条件 2 次
    「App 名」、操作步骤多个「UI 元素」 —— 改前抓到出现位置最早的 UI 元素，
    改后只在前置条件段抽，必须返回真正的 App 名。
    """
    goal = (
        "测试标题：【正向】首次进入页时卡片为默认展开态显示「详情」\n"
        "前置条件：关闭 App「淘宝」（杀进程）后重新打开 App「淘宝」；"
        "如果未登录则使用账号登录；非业务弹窗允许自动关闭\n"
        "操作步骤：从首页底部 Tab 进入「我的」、点击「设置」入口、"
        "下滑至底部点击「关于」按钮进入次级页\n"
        "预期结果：卡片右下角显示蓝色「详情」链接"
    )
    assert _detect_app_lifecycle_prelude(goal) == "淘宝"


def test_detect_prelude_no_trigger_when_only_in_operation_step():
    """关闭+重启关键词只出现在『操作步骤』段、前置条件段无此动作 —— 起跑线
    必须放弃，让 VLM 主循环按顺序执行业务步骤，避免提前偷跑+重复执行。

    注意 goal 中前置条件段刻意不带任何 ``「」``，避免触发档 2 的
    "启动词紧贴「」" 边界 bug（见
    ``test_detect_prelude_passive_state_in_precondition_xfail``）。
    """
    goal = (
        "测试标题：验证某开关在杀进程后能否持久化\n"
        "前置条件：已完成账号登录，进度无残留\n"
        "操作步骤：1. 进入设置页修改某开关为关闭；"
        "2. 关闭 App「微信」；3. 重新打开 App「微信」；"
        "4. 验证某开关仍为关闭\n"
        "预期结果：开关保持关闭状态"
    )
    assert _detect_app_lifecycle_prelude(goal) is None


@pytest.mark.xfail(
    reason="档 2 的 _OPEN_APP_TIGHT_RE 无法区分'已进入「X」'(状态描述) 与 "
    "'进入「X」'(启动意图)。属于档 2 独立边界 bug，不在按段感知改造范围内；"
    "未来需在启动词正则前补一层'状态词否定后顾'(已/正/正在/位于/处于…)。",
    strict=True,
)
def test_detect_prelude_passive_state_in_precondition_xfail():
    """档 2 既有边界问题：前置条件段写"已进入「设置」页"被误判为启动「设置」。"""
    goal = (
        "测试标题：验证某开关持久化\n"
        "前置条件：已登录账号，已进入「设置」页\n"
        "操作步骤：1. 修改开关；2. 关闭 App「微信」；"
        "3. 重新打开 App「微信」；4. 验证开关\n"
        "预期结果：开关保持原状"
    )
    assert _detect_app_lifecycle_prelude(goal) is None


def test_detect_prelude_standard_precondition_still_triggers():
    """标准前置条件场景：前置条件段有"关闭+重启 X"，操作步骤段无此动作 ——
    行为与重构前一致，起跑线照常触发。
    """
    goal = (
        "测试标题：登录主流程验证\n"
        "前置条件：杀进程并重新打开「淘宝」\n"
        "操作步骤：输入手机号、获取验证码、点击登录\n"
        "预期结果：成功进入首页"
    )
    assert _detect_app_lifecycle_prelude(goal) == "淘宝"


def test_detect_prelude_freeform_goal_falls_back_to_fulltext_scan():
    """自由对话 / 平铺型 goal（无任何段头标签）—— 退到原"全文扫"逻辑，
    短句"杀掉 X 重新打开"或"打开 X 做 Y"都应正确触发。
    """
    # 档 1：杀进程 + 重启 + 「App」 → 命中
    assert _detect_app_lifecycle_prelude("杀掉「淘宝」重新打开做下单") == "淘宝"
    # 档 2：启动词紧贴「App」 → 命中
    assert _detect_app_lifecycle_prelude("打开「微信」找联系人") == "微信"


def test_detect_prelude_returns_none_when_no_app_or_no_trigger():
    """边界用例：空 goal / 仅有标签无关键词 / 仅有 app 名无触发词 —— 全部返回 None。"""
    assert _detect_app_lifecycle_prelude("") is None
    assert _detect_app_lifecycle_prelude("前置条件：已登录\n操作步骤：浏览商品") is None
    # 前置条件段提到 app 但没"关闭+重启"也没"启动词紧贴"，不该触发
    assert (
        _detect_app_lifecycle_prelude(
            "前置条件：已在「淘宝」首页\n操作步骤：搜索 iPhone"
        )
        is None
    )


def test_detect_prelude_precondition_overrides_freeform_fulltext_noise():
    """前置条件段命中时，**不能**因 fallback 而被全文 noise 污染——
    回归保护：旧实现一旦命中关键词就扫全文，会把测试标题里偶提的引号
    内容也算进频次。新实现保证只看前置条件段。
    """
    goal = (
        "测试标题：在「设置」「关于」「版本号」三个入口都能看到统一品牌名\n"
        "前置条件：杀掉「微信」并重新打开\n"
        "操作步骤：进入设置→关于→查看版本号"
    )
    # 测试标题里 3 个 UI 元素引号，前置条件里只有 1 个 app。
    # 全文扫会因为「设置」「关于」「版本号」 vs「微信」频次（1:1:1:1）按
    # 出现位置抓到"设置"。新实现只看前置条件段，必须抽到"微信"。
    assert _detect_app_lifecycle_prelude(goal) == "微信"
