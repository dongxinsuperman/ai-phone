"""parse_and_validate 准入校验单测。

覆盖 v1.7 一种 body 形态的所有合法 / 非法路径。包含：

- wrapper 顶层（必须是 object）
- platforms 必填、非空、合法、不重复
- deviceAliasPools 可选；为对象时 key 合法性 + value 是数组
- 池语义：缺省 / null / [] / 单别名 / 多别名 → ItemDraft.device_alias_pool 规整
- 拆条：一条多端 raw item 展开成 N 条 ItemDraft
- v1.8 新增：callbackUrl 可选、必须 http(s)、长度上限 1024
"""
from __future__ import annotations

import pytest

from ai_phone.server.scheduler.service import (
    AdmissionError,
    parse_function_map_context,
    parse_and_validate,
)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_minimal_single_platform_no_pool():
    """最小合法 body：platforms 单端、不传 deviceAliasPools。"""
    name, callback_url, _retry, drafts = parse_and_validate({
        "submissionName": "smoke",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "do something",
                "platforms": ["android"],
            }
        ],
    })
    assert name == "smoke"
    assert callback_url is None
    assert len(drafts) == 1
    d = drafts[0]
    assert d.case_id == "C-1"
    assert d.platform == "android"
    assert d.run_content == "do something"
    assert d.device_alias_pool is None  # 全池任挑
    assert d.case_name is None
    assert d.function_map_context == ""


def test_function_map_context_top_level_normalized():
    text = parse_function_map_context(
        {
            "functionMapContext": "  首页：底部有我的 Tab\n账号：demo  ",
            "items": [],
        },
        max_chars=100,
    )

    assert text == "首页：底部有我的 Tab\n账号：demo"


def test_function_map_context_rejects_too_long():
    with pytest.raises(AdmissionError) as exc_info:
        parse_function_map_context(
            {"functionMapContext": "abcd"},
            max_chars=3,
        )

    assert exc_info.value.reason == "function_map_context_too_long"
    assert "3 字符上限" in exc_info.value.detail


def test_item_function_map_context_fans_out_to_all_platforms():
    """一条 raw item 多端裂变时，item 级 function map 复制到每个执行单元。"""
    _name, _cb, _retry, drafts = parse_and_validate(
        {
            "submissionName": "fanout-map",
            "items": [
                {
                    "caseId": "pay-status",
                    "runContent": "check pay status",
                    "platforms": ["android", "ios"],
                    "functionMapContext": "支付页入口在 我的-订单-待支付",
                }
            ],
        },
        function_map_context_max_chars=100,
    )

    assert {d.platform for d in drafts} == {"android", "ios"}
    assert {d.function_map_context for d in drafts} == {"支付页入口在 我的-订单-待支付"}


def test_item_function_map_context_rejects_non_string():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate(
            {
                "submissionName": "bad-map",
                "items": [
                    {
                        "caseId": "C-1",
                        "runContent": "x",
                        "platforms": ["android"],
                        "functionMapContext": {"bad": "value"},
                    }
                ],
            },
            function_map_context_max_chars=100,
        )

    assert exc_info.value.reason == "invalid_body"
    assert exc_info.value.index == 0
    assert "functionMapContext 必须是字符串" in exc_info.value.detail


def test_multi_platform_fanout_no_pool():
    """一条 raw item 多端 → 拆 N 条 ItemDraft，platform 一一对应。"""
    name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "fanout",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android", "ios", "harmony"],
            }
        ],
    })
    assert {d.platform for d in drafts} == {"android", "ios", "harmony"}
    assert len(drafts) == 3
    for d in drafts:
        assert d.device_alias_pool is None


def test_pool_size_one_locks_single():
    """长度 1 的池：等价于"锁单台"语义。"""
    _name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "lock-single",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": {"android": ["A1"]},
            }
        ],
    })
    assert drafts[0].device_alias_pool == ["A1"]


def test_pool_size_n_subset_pool():
    """长度 N 的池：场景 5 子集池；写入 ItemDraft 时 dedup + sorted。"""
    _name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "subset",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": {"android": ["B1", "A1", "A1"]},
            }
        ],
    })
    # 去重 + 排序
    assert drafts[0].device_alias_pool == ["A1", "B1"]


def test_pool_partial_per_platform():
    """多端时仅指定一端的池：未指定的端走全池任挑。"""
    _name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "partial",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android", "ios"],
                "deviceAliasPools": {"android": ["A1", "B1"]},
            }
        ],
    })
    by_p = {d.platform: d for d in drafts}
    assert by_p["android"].device_alias_pool == ["A1", "B1"]
    assert by_p["ios"].device_alias_pool is None


def test_pool_explicit_null_or_empty_means_full_pool():
    """deviceAliasPools[p] 显式 null / [] = 该端全池任挑（落 None）。"""
    _name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "null-pool",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android", "ios"],
                "deviceAliasPools": {"android": None, "ios": []},
            }
        ],
    })
    for d in drafts:
        assert d.device_alias_pool is None


def test_case_name_passthrough():
    _name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "smoke",
        "items": [
            {
                "caseId": "C-1",
                "caseName": "登录用例",
                "runContent": "x",
                "platforms": ["android"],
            }
        ],
    })
    assert drafts[0].case_name == "登录用例"


# ---------------------------------------------------------------------------
# 顶层错误
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        [],  # 裸数组（v1.7 已废）
        "string",
        123,
        None,
    ],
)
def test_top_level_must_be_object(body):
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate(body)
    assert exc_info.value.reason == "invalid_body"


def test_items_must_be_list():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({"submissionName": "x", "items": "not-a-list"})
    assert exc_info.value.reason == "invalid_body"


def test_items_empty():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({"submissionName": "x", "items": []})
    assert exc_info.value.reason == "invalid_body"


def test_item_not_object():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({"submissionName": "x", "items": ["not-an-object"]})
    assert exc_info.value.reason == "invalid_body"
    assert exc_info.value.index == 0


# ---------------------------------------------------------------------------
# 字段缺失 / 非法
# ---------------------------------------------------------------------------


def test_missing_caseId():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{"runContent": "x", "platforms": ["android"]}],
        })
    assert exc_info.value.reason == "missing_field"


def test_missing_runContent():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{"caseId": "C-1", "platforms": ["android"]}],
        })
    assert exc_info.value.reason == "missing_field"


def test_missing_platforms():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{"caseId": "C-1", "runContent": "x"}],
        })
    assert exc_info.value.reason == "missing_field"


def test_platforms_not_list():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": "android",
            }],
        })
    assert exc_info.value.reason == "invalid_body"


def test_platforms_empty():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": [],
            }],
        })
    assert exc_info.value.reason == "invalid_body"


def test_platforms_invalid_value():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["windows"],
            }],
        })
    assert exc_info.value.reason == "invalid_platform"


def test_platforms_duplicate():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android", "android"],
            }],
        })
    assert exc_info.value.reason == "invalid_body"


# ---------------------------------------------------------------------------
# deviceAliasPools 错误路径
# ---------------------------------------------------------------------------


def test_pools_not_object():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": ["A1"],
            }],
        })
    assert exc_info.value.reason == "invalid_body"


def test_pools_invalid_platform_key():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": {"windows": ["A1"]},
            }],
        })
    assert exc_info.value.reason == "invalid_platform"


def test_pools_key_not_in_platforms():
    """deviceAliasPools 的 key 是合法平台但不在本条 platforms 里 → 拒。"""
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": {"ios": ["I1"]},
            }],
        })
    assert exc_info.value.reason == "pool_alias_not_in_platforms"


def test_pool_value_not_list():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": {"android": "A1"},
            }],
        })
    assert exc_info.value.reason == "invalid_body"


def test_pool_contains_empty_alias():
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "items": [{
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
                "deviceAliasPools": {"android": ["A1", ""]},
            }],
        })
    assert exc_info.value.reason == "invalid_body"


# ---------------------------------------------------------------------------
# v1.8 callbackUrl 校验
# ---------------------------------------------------------------------------


def test_callback_url_happy_path():
    """合法 https URL → 正常解析。"""
    _name, callback_url, _retry, _drafts = parse_and_validate({
        "submissionName": "x",
        "callbackUrl": "https://my-server.example.com/cb",
        "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
    })
    assert callback_url == "https://my-server.example.com/cb"


def test_callback_url_empty_string_treated_as_absent():
    """空串 / 空白 = 没传，callback_url 落 None。"""
    _name, callback_url, _retry, _drafts = parse_and_validate({
        "submissionName": "x",
        "callbackUrl": "",
        "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
    })
    assert callback_url is None


# ---------------------------------------------------------------------------
# retryMax 解析
# ---------------------------------------------------------------------------


def test_retry_max_top_level_parsed():
    """retryMax 是顶层批次策略，合法整数原样返回。"""
    _name, _cb, retry_max, drafts = parse_and_validate({
        "submissionName": "retry",
        "retryMax": 2,
        "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
    })
    assert retry_max == 2
    assert len(drafts) == 1


def test_retry_max_invalid_values_coerce_to_zero():
    """非法 / 负数 / bool 按方案静默归 0，避免准入失败。"""
    for value in ("abc", -1, True):
        _name, _cb, retry_max, _drafts = parse_and_validate({
            "submissionName": "retry",
            "retryMax": value,
            "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
        })
        assert retry_max == 0


def test_callback_url_must_be_http_or_https():
    """非 http(s) 协议 → invalid_body。"""
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "callbackUrl": "ftp://example.com/cb",
            "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
        })
    assert exc_info.value.reason == "invalid_body"


def test_callback_url_too_long():
    """长度 > 1024 → invalid_body。"""
    long_url = "https://" + ("x" * 1100)
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "callbackUrl": long_url,
            "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
        })
    assert exc_info.value.reason == "invalid_body"


def test_callback_url_must_be_string():
    """非字符串类型 → invalid_body。"""
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate({
            "submissionName": "x",
            "callbackUrl": 12345,
            "items": [{"caseId": "C-1", "runContent": "x", "platforms": ["android"]}],
        })
    assert exc_info.value.reason == "invalid_body"


# ---------------------------------------------------------------------------
# 边界场景
# ---------------------------------------------------------------------------


def test_index_propagates_to_failing_item():
    """非首条 item 出错时 index 指向正确位置。"""
    body = {
        "submissionName": "x",
        "items": [
            {
                "caseId": "C-1",
                "runContent": "x",
                "platforms": ["android"],
            },
            {
                "caseId": "C-2",
                "runContent": "x",
                "platforms": ["windows"],  # 非法
            },
        ],
    }
    with pytest.raises(AdmissionError) as exc_info:
        parse_and_validate(body)
    assert exc_info.value.reason == "invalid_platform"
    assert exc_info.value.index == 1


def test_submission_name_can_be_empty():
    """submissionName 为空时不拒，调用方按需回落 submission_id。"""
    name, _cb, _retry, _drafts = parse_and_validate({
        "submissionName": "",
        "items": [{
            "caseId": "C-1",
            "runContent": "x",
            "platforms": ["android"],
        }],
    })
    assert name == ""


def test_extra_fields_silently_ignored():
    """无关字段不报错。"""
    name, _cb, _retry, drafts = parse_and_validate({
        "submissionName": "x",
        "extraTopField": "ignored",
        "items": [{
            "caseId": "C-1",
            "runContent": "x",
            "platforms": ["android"],
            "extraField": "ignored",
        }],
    })
    assert name == "x"
    assert len(drafts) == 1
