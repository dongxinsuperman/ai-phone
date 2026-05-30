from ai_phone.agent.ws_client import normalize_server_address


def test_normalize_http_base_to_ws_and_http():
    ws, http = normalize_server_address("http://server.example.com:8000")
    assert ws == "ws://server.example.com:8000/ws/agent"
    assert http == "http://server.example.com:8000"


def test_normalize_https_base_to_wss():
    ws, http = normalize_server_address("https://aiphone.example.com")
    assert ws == "wss://aiphone.example.com/ws/agent"
    assert http == "https://aiphone.example.com"


def test_normalize_host_without_scheme_defaults_to_http():
    ws, http = normalize_server_address("10.8.8.120:8000")
    assert ws == "ws://10.8.8.120:8000/ws/agent"
    assert http == "http://10.8.8.120:8000"


def test_normalize_existing_ws_url_and_derives_http_base():
    ws, http = normalize_server_address("wss://aiphone.example.com/ws/agent")
    assert ws == "wss://aiphone.example.com/ws/agent"
    assert http == "https://aiphone.example.com"


def test_normalize_reverse_proxy_base_path():
    ws, http = normalize_server_address("https://example.com/aiphone")
    assert ws == "wss://example.com/aiphone/ws/agent"
    assert http == "https://example.com/aiphone"
