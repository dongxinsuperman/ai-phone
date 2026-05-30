from ai_phone.agent.drivers import hdc as hdc_mod


def _parse_targets(monkeypatch, raw: str):
    monkeypatch.setattr(hdc_mod, "hdc_run", lambda *args, **kwargs: raw)
    return hdc_mod.hdc_list_targets()


def test_hdc_list_targets_ignores_empty_with_orphan_hdc(monkeypatch):
    assert _parse_targets(monkeypatch, "[Empty]\r\thdc") == []


def test_hdc_list_targets_ignores_ansi_warning_lines(monkeypatch):
    raw = "[\x1b[1;33mW\x1b[0m][2026-05-21 19:00:00] hmdriver2 warning"
    assert _parse_targets(monkeypatch, raw) == []


def test_hdc_list_targets_ignores_server_connect_failure(monkeypatch):
    assert _parse_targets(monkeypatch, "Connect server failed") == []


def test_hdc_list_targets_accepts_single_column_targets(monkeypatch):
    targets = _parse_targets(monkeypatch, "ABC123\nDEF_456-7\n192.168.0.2:8710")
    assert [(t.serial, t.status) for t in targets] == [
        ("ABC123", "Connected"),
        ("DEF_456-7", "Connected"),
        ("192.168.0.2:8710", "Connected"),
    ]


def test_hdc_list_targets_accepts_verbose_targets(monkeypatch):
    raw = "\n".join(
        [
            "ABC123 USB Connected hwmate60 HarmonyOS",
            "DEF456 USB Offline hwmate60 HarmonyOS",
            "GHI789 USB Unauthorized hwmate60 HarmonyOS",
        ]
    )
    targets = _parse_targets(monkeypatch, raw)
    assert [(t.serial, t.status) for t in targets] == [
        ("ABC123", "Connected"),
        ("DEF456", "Offline"),
        ("GHI789", "Unauthorized"),
    ]
