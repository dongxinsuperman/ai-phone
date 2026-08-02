"""iOS 虚拟机数据层：官方目录、机型区间校验、建表与导入隔离。"""
import pytest

from ai_phone.server.ios_sim import catalog as cat
from ai_phone.server.ios_sim.models import IOS_SIM_TABLES, IosSimVmInstance


@pytest.fixture(autouse=True)
def _clean_cache():
    cat.reset_cache_for_tests()
    yield
    cat.reset_cache_for_tests()


# --------------------------------------------------------------------------
# bundle 的官方目录
# --------------------------------------------------------------------------
def test_bundled_catalog_loads():
    payload = cat.load_bundled_catalog()
    assert payload["device_types"], "目录里必须有机型"
    assert payload["source"].startswith("xcrun simctl")


def test_catalog_only_contains_iphone_and_ipad():
    families = {dt["product_family"] for dt in cat.device_types()}
    assert families == {"iPhone", "iPad"}


def test_every_device_type_carries_version_range():
    """机型 × 系统版本的合法区间是这套设计的地基，缺一个都不行。"""
    for dt in cat.device_types():
        assert dt["identifier"]
        assert dt["name"]
        assert dt["min_runtime_version"] > 0
        assert dt["max_runtime_version"] > 0
        assert dt["min_runtime_version_string"]
        assert dt["max_runtime_version_string"]


def test_catalog_summary():
    s = cat.catalog_summary()
    assert s["device_type_count"] == len(cat.device_types())
    assert s["families"]["iPhone"] > 0
    assert s["families"]["iPad"] > 0


# --------------------------------------------------------------------------
# 系统版本清单
#
# 第一版这里踩过一个坑：版本清单是从导出脚本所在机器的
# `simctl list runtimes` 抓的，那台机器只装了一个 runtime，于是整个平台就只能
# 建 iOS 26 的虚拟机。现在改成从苹果官方下载索引取全量已发布版本。
# --------------------------------------------------------------------------
def test_official_runtimes_cover_many_versions():
    """版本清单必须是官方全量，不能退化成某台机器的已装列表。"""
    runtimes = cat.load_bundled_catalog().get("official_runtimes") or []
    assert len(runtimes) >= 20, (
        f"只有 {len(runtimes)} 个系统版本，像是又退回成「导出机器已装的 runtime」了；"
        "应当来自苹果官方下载索引"
    )


def test_official_runtimes_have_wide_major_span():
    majors = {
        int(rt["version"].split(".")[0])
        for rt in cat.load_bundled_catalog().get("official_runtimes") or []
    }
    assert len(majors) >= 8, f"大版本跨度太窄：{sorted(majors)}"


def test_runtime_identifier_is_major_minor_only():
    """runtime identifier 只到次版本——26.0 与 26.0.1 是同一个 runtime。"""
    for rt in cat.load_bundled_catalog().get("official_runtimes") or []:
        tail = rt["identifier"].rsplit(".", 1)[-1]
        assert tail.startswith("iOS-"), rt["identifier"]
        parts = tail.split("-")
        assert len(parts) == 3, f"identifier 应形如 iOS-26-0：{rt['identifier']}"
        major, minor = rt["version"].split(".")[:2]
        assert parts[1] == major and parts[2] == minor, rt


def test_runtime_identifiers_are_unique():
    runtimes = cat.load_bundled_catalog().get("official_runtimes") or []
    ids = [rt["identifier"] for rt in runtimes]
    assert len(ids) == len(set(ids)), "同一个 runtime identifier 出现了多次"


def test_official_runtimes_exclude_prereleases():
    """beta / RC 不进目录：设备农场要可复现，预发布构建号随时会被替换。"""
    for rt in cat.load_bundled_catalog().get("official_runtimes") or []:
        lowered = f"{rt['name']} {rt['version']}".lower()
        assert "beta" not in lowered and "rc" not in lowered, rt


def test_every_runtime_matches_some_device_type():
    """目录里不该有任何机型都用不上的版本。"""
    dts = cat.device_types()
    for rt in cat.load_bundled_catalog().get("official_runtimes") or []:
        assert any(cat.supports_version(dt, rt["version"]) for dt in dts), (
            f"没有任何机型支持 {rt['name']}"
        )


def test_modern_device_type_has_multiple_runtime_choices():
    """随手挑一台新机型，可选版本必须不止一个——否则前端下拉又变成单选。"""
    dt = next(
        d for d in cat.device_types() if d["name"] == "iPhone 16e"
    )
    usable = [
        rt
        for rt in cat.load_bundled_catalog().get("official_runtimes") or []
        if cat.supports_version(dt, rt["version"])
    ]
    assert len(usable) >= 3, f"iPhone 16e 只有 {len(usable)} 个可选版本"


# --------------------------------------------------------------------------
# 版本编码：与苹果的整数编码严格一致（实测校验过）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,value",
    [("26.0.0", 1703936), ("16.9.0", 1050880), ("11.0.0", 720896)],
)
def test_version_encode_decode_roundtrip(text, value):
    assert cat.encode_version(text) == value
    assert cat.decode_version(value) == text


def test_encode_tolerates_short_and_bad_versions():
    assert cat.encode_version("26") == 26 << 16
    assert cat.encode_version("26.0") == 26 << 16
    assert cat.encode_version("") == 0
    assert cat.encode_version("不是版本") == 0


# --------------------------------------------------------------------------
# 区间校验
# --------------------------------------------------------------------------
def _dt(identifier="X", low="11.0.0", high="16.9.0"):
    return {
        "identifier": identifier,
        "min_runtime_version": cat.encode_version(low),
        "max_runtime_version": cat.encode_version(high),
    }


def test_supports_version_within_range():
    dt = _dt()
    assert cat.supports_version(dt, "15.0.0") is True
    assert cat.supports_version(dt, "11.0.0") is True
    assert cat.supports_version(dt, "16.9.0") is True


def test_rejects_below_min_and_above_max():
    dt = _dt()
    assert cat.supports_version(dt, "10.0.0") is False
    assert cat.supports_version(dt, "26.0.0") is False


def test_rejects_unparsable_version():
    assert cat.supports_version(_dt(), "") is False


def test_real_iphone_8_cannot_take_ios_26():
    """真实数据回归：iPhone 8 官方上限 16.9，装不上 iOS 26（M3-b 实测过的场景）。"""
    dt = cat.find_device_type("com.apple.CoreSimulator.SimDeviceType.iPhone-8")
    assert dt is not None
    assert cat.supports_version(dt, "16.4.0") is True
    assert cat.supports_version(dt, "26.0.0") is False


def test_compatible_device_types_filters():
    compatible = cat.compatible_device_types("26.0.0")
    names = {dt["name"] for dt in compatible}
    assert "iPhone 8" not in names           # 上限 16.9
    assert any(n.startswith("iPhone 17") for n in names)


def test_find_device_type_returns_none_for_unknown():
    assert cat.find_device_type("nope") is None


# --------------------------------------------------------------------------
# 表结构
# --------------------------------------------------------------------------
def test_only_two_tables():
    """相比鸿蒙少两张表：无端口租约、无 settings（方案 §6.5.5）。"""
    names = {t.name for t in IOS_SIM_TABLES}
    assert names == {"ios_sim_vm_instances", "ios_sim_catalog_snapshots"}


def test_instance_table_has_no_lease_columns():
    cols = {c.name for c in IosSimVmInstance.__table__.columns}
    for forbidden in ("lease_token", "hdc_port", "adb_serial"):
        assert forbidden not in cols
    # 设备身份是 UDID
    assert "udid" in cols


def test_instance_defaults_to_draft():
    inst = IosSimVmInstance(name="n", alias="a")
    assert inst.state is None or inst.state == "draft"  # 默认在 flush 时生效


def test_to_dict_exposes_expected_keys():
    inst = IosSimVmInstance(
        id="abc", name="n", alias="a",
        device_type="dt", device_type_name="iPhone 17 Pro",
        runtime="rt", runtime_name="iOS 26.0", os_version="26.0.1",
        state="running", udid="U", wda_port=8300, mjpeg_port=9300,
    )
    d = inst.to_dict()
    for key in (
        "id", "alias", "device_type", "device_type_name", "runtime",
        "runtime_name", "os_version", "state", "udid", "wda_port", "mjpeg_port",
    ):
        assert key in d
    assert d["device_type_name"] == "iPhone 17 Pro"


# --------------------------------------------------------------------------
# 目录缺失必须炸，不能静默降级成空目录
# --------------------------------------------------------------------------
def test_missing_catalog_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(cat, "_BUNDLED", tmp_path / "nope.json")
    cat.reset_cache_for_tests()
    with pytest.raises(cat.CatalogError):
        cat.load_bundled_catalog()


def test_empty_catalog_raises(monkeypatch, tmp_path):
    bad = tmp_path / "empty.json"
    bad.write_text('{"device_types": []}', encoding="utf-8")
    monkeypatch.setattr(cat, "_BUNDLED", bad)
    cat.reset_cache_for_tests()
    with pytest.raises(cat.CatalogError):
        cat.load_bundled_catalog()


def test_malformed_catalog_raises(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("不是 JSON", encoding="utf-8")
    monkeypatch.setattr(cat, "_BUNDLED", bad)
    cat.reset_cache_for_tests()
    with pytest.raises(cat.CatalogError):
        cat.load_bundled_catalog()
