"""内部通道（platform）与对外平台（platform_family）的两层模型。

约束：
- 内部四个通道，对外三个平台，只有 iOS 分叉
- 家族关系是**设备的属性**，定义在协议层，调度与接口共用同一份
- 派单展开与家族归属必须互为反向，不能各写各的
"""
import pytest

from ai_phone.shared import protocol as P


# --------------------------------------------------------------------------
# 正向：内部通道 → 对外平台
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "platform,family",
    [
        ("android", "android"),
        ("ios", "ios"),
        ("ios_sim", "ios"),
        ("harmony", "harmony"),
    ],
)
def test_platform_family(platform, family):
    assert P.platform_family(platform) == family


def test_only_ios_forks():
    """只有 iOS 允许出现内部通道与对外平台不一致。"""
    forked = [p for p, f in P.PLATFORM_FAMILY.items() if p != f]
    assert forked == ["ios_sim"]


def test_families_are_exactly_three():
    assert set(P.PLATFORM_FAMILY.values()) == {"android", "ios", "harmony"}


def test_every_protocol_platform_has_a_family():
    """Platform Literal 里新增了值却忘了归类，会让它在派单和界面上双双消失。"""
    from typing import get_args

    for platform in get_args(P.Platform):
        assert platform in P.PLATFORM_FAMILY, f"{platform} 没有归属的对外平台"


def test_unknown_platform_is_returned_as_is():
    """未知值不猜——猜错会把一台设备静默归到错误的平台池。"""
    assert P.platform_family("tizen") == "tizen"
    assert P.platform_family("") == ""


# --------------------------------------------------------------------------
# 反向：对外平台 → 内部通道
# --------------------------------------------------------------------------
def test_family_platforms():
    assert P.family_platforms("ios") == ("ios", "ios_sim")
    assert P.family_platforms("android") == ("android",)
    assert P.family_platforms("harmony") == ("harmony",)


def test_mappings_are_mutually_inverse():
    """两张表必须互为反向，否则「界面说它是 iOS、派单却选不到」。"""
    for family, platforms in P.FAMILY_PLATFORMS.items():
        for platform in platforms:
            assert P.platform_family(platform) == family
    for platform, family in P.PLATFORM_FAMILY.items():
        assert platform in P.FAMILY_PLATFORMS[family]


def test_scheduler_reuses_protocol_mapping():
    """调度器不得自己维护一份——那会变成看不见的隐性知识。"""
    from ai_phone.server.scheduler.service import device_platforms_for

    for family in P.FAMILY_PLATFORMS:
        assert device_platforms_for(family) == P.family_platforms(family)


def test_submission_platforms_match_families():
    """提交接口的平台枚举 == 对外平台集合。"""
    from ai_phone.server.scheduler.service import ALLOWED_PLATFORMS

    assert set(ALLOWED_PLATFORMS) == set(P.FAMILY_PLATFORMS)


# --------------------------------------------------------------------------
# 设备数据里要带上家族，下游不必各自去猜
# --------------------------------------------------------------------------
def test_agent_device_info_carries_family():
    from ai_phone.agent.drivers.base import DeviceInfo

    d = DeviceInfo(serial="SIM-1", platform="ios_sim").to_dict()
    assert d["platform"] == "ios_sim", "内部通道必须原样保留"
    assert d["platform_family"] == "ios"


def test_agent_device_info_family_for_real_device():
    from ai_phone.agent.drivers.base import DeviceInfo

    d = DeviceInfo(serial="R1", platform="ios").to_dict()
    assert d["platform"] == "ios" and d["platform_family"] == "ios"


def test_server_device_dict_carries_family():
    from ai_phone.server.models import Device

    d = Device(serial="SIM-1", platform="ios_sim").to_dict()
    assert d["platform"] == "ios_sim"
    assert d["platform_family"] == "ios"


def test_server_device_dict_family_for_other_platforms():
    from ai_phone.server.models import Device

    for platform in ("android", "harmony", "ios"):
        d = Device(serial="X", platform=platform).to_dict()
        assert d["platform_family"] == platform


def test_family_is_derived_not_stored():
    """不落库：它是恒定映射，存一列只会多出可能与 platform 不一致的冗余。"""
    from ai_phone.server.models import Device

    assert "platform_family" not in Device.__table__.columns


# --------------------------------------------------------------------------
# 所有「拿设备 platform 跟提交声明的平台作比较」的地方都必须先折算
#
# 漏一处的症状很隐蔽：设备在线、界面正常，但提交被拒或别名被判平台不符。
# 已经踩过两次（准入校验、别名校验），这里做源码级兜底。
# --------------------------------------------------------------------------
def test_every_platform_comparison_site_converts_to_family():
    """读 Device.platform 后与外部平台比较的地方，必须过 platform_family。"""
    import inspect

    from ai_phone.server.aliases import service as alias_svc
    from ai_phone.server.scheduler import service as sched_svc

    admission = inspect.getsource(sched_svc.SubmissionScheduler._online_platforms)
    assert "platform_family" in admission, "准入校验没折算成对外平台"

    alias_src = inspect.getsource(alias_svc)
    block = alias_src.split("serial_to_platform")[1][:400]
    assert "platform_family" in block, "别名平台校验没折算成对外平台"


def test_app_install_deliberately_stays_strict():
    """应用分发**刻意不折算**：.ipa 装不进虚拟机，两种包各配各的设备。"""
    import inspect

    from ai_phone.server.app_install import service as install_svc

    src = inspect.getsource(install_svc.eligible_devices)
    assert "Device.platform == pkg.platform" in src
