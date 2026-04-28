#!/usr/bin/env python3
"""单文件版 iOS 真机点击演示。

这个脚本的目标不是“接入整个 ai-phone 平台”，而是把最小可验证链路完整写在一个
文件里，让你能顺着代码看清楚：

1. 先通过 Xcode 工具链把 WebDriverAgent (WDA) 作为 XCTest UI Test 跑起来
2. 再把真机里的 8100 端口通过 usbmux 转发到本机
3. 然后调 WDA 的 ``/status``、``/session``、``/wda/tap/0``
4. 最终让 iPhone 真机执行一次点击

重要认知：

- 这里的“用 Xcode”指的是用 ``xcodebuild`` 这套官方工具链，不是要求你一直开
  Xcode 图形界面。
- WDA 不是普通 app。它必须跑在 XCTest/XCUITest 测试会话里，才能拿到 UI 自动化
  权限。
- 所以“装一个 ipa”只是必要条件之一；真正关键的是“能不能把 XCTest 会话拉起来”。

这个脚本默认依赖：

- macOS + 已安装 Xcode
- Python 3.11+
- ``pymobiledevice3``（用于 usbmux 端口转发和枚举设备）
- 你本地已经有一份 WebDriverAgent Xcode 工程源码
  例如自己提前 clone 到某个目录：
  ``git clone https://github.com/appium/WebDriverAgent.git``

推荐运行方式：

    cd ai-phone
    ../ai-phone/backend/.venv/bin/python ios_wda_xcode_tap_demo.py --list-devices

    ../ai-phone/backend/.venv/bin/python ios_wda_xcode_tap_demo.py \
      --project ~/code/WebDriverAgent/WebDriverAgent.xcodeproj \
      --udid 00008150-00041CAE3478401C \
      --team-id YOUR_TEAM_ID \
      --bundle-id com.yourname.WebDriverAgentRunner

如果不传 ``--x`` / ``--y``，默认点击屏幕中心。

注意：

- ``--x`` / ``--y`` 使用的是 WDA 逻辑坐标（point），不是物理像素。
- 如果你只想验证“WDA 是否真的跑起来”，可以先不传 ``--x`` / ``--y``，看脚本能
  否走到 ``/status`` 与 ``create session``。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest


WDA_DEVICE_PORT = 8100


class DemoError(RuntimeError):
    """脚本级错误。"""


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def ensure_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DemoError(
            f"找不到命令 `{name}`。请先安装它，再重试。"
        )
    return path


def ensure_xcodebuild() -> str:
    xcodebuild = ensure_tool("xcodebuild")
    try:
        out = subprocess.check_output(
            [xcodebuild, "-version"], text=True, stderr=subprocess.STDOUT
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise DemoError(f"`xcodebuild -version` 执行失败：{exc.output}") from exc
    log(f"检测到 Xcode 工具链：{out.replace(chr(10), ' | ')}")
    return xcodebuild


def require_pymobiledevice3() -> None:
    try:
        import pymobiledevice3  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise DemoError(
            "缺少 pymobiledevice3。建议用 ai-phone/backend/.venv 运行，"
            "或先安装：pip install pymobiledevice3"
        ) from exc


# ---------------------------------------------------------------------------
# pymobiledevice3 async -> sync 桥
# ---------------------------------------------------------------------------
_PMD3_LOOP: Optional[asyncio.AbstractEventLoop] = None
_PMD3_LOOP_LOCK = threading.Lock()


def _get_pmd3_loop() -> asyncio.AbstractEventLoop:
    global _PMD3_LOOP  # noqa: PLW0603
    with _PMD3_LOOP_LOCK:
        if _PMD3_LOOP is not None and not _PMD3_LOOP.is_closed():
            return _PMD3_LOOP
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                loop.close()

        threading.Thread(target=_runner, daemon=True, name="pmd3-loop").start()
        _PMD3_LOOP = loop
        return loop


async def _await_it(awaitable: Any) -> Any:
    return await awaitable


def maybe_sync(value: Any, timeout: float = 30.0) -> Any:
    if not inspect.isawaitable(value):
        return value
    fut = asyncio.run_coroutine_threadsafe(_await_it(value), _get_pmd3_loop())
    return fut.result(timeout=timeout)


# ---------------------------------------------------------------------------
# 设备发现
# ---------------------------------------------------------------------------
@dataclass
class ConnectedDevice:
    udid: str
    name: str
    product_version: str
    model: str


def _lockdown_factory():
    from pymobiledevice3 import lockdown as lockdown_mod

    if hasattr(lockdown_mod, "create_using_usbmux"):
        return lockdown_mod.create_using_usbmux
    return lockdown_mod.LockdownClient  # 老版本 fallback


def list_connected_devices() -> list[ConnectedDevice]:
    require_pymobiledevice3()
    from pymobiledevice3 import usbmux

    create_lockdown = _lockdown_factory()
    devices = maybe_sync(usbmux.list_devices()) or []
    result: list[ConnectedDevice] = []
    for dev in devices:
        udid = getattr(dev, "serial", None) or getattr(dev, "udid", None)
        if not udid:
            continue
        name = ""
        version = ""
        model = ""
        try:
            ld = maybe_sync(create_lockdown(serial=udid))
            if ld is not None:
                name = str(maybe_sync(ld.get_value(key="DeviceName")) or "")
                version = str(maybe_sync(ld.get_value(key="ProductVersion")) or "")
                model = str(maybe_sync(ld.get_value(key="ProductType")) or "")
        except Exception:  # noqa: BLE001
            pass
        result.append(
            ConnectedDevice(
                udid=str(udid),
                name=name or "(unknown)",
                product_version=version or "(unknown)",
                model=model or "(unknown)",
            )
        )
    return result


def choose_udid(explicit_udid: Optional[str]) -> str:
    if explicit_udid:
        return explicit_udid
    devices = list_connected_devices()
    if not devices:
        raise DemoError(
            "没有发现通过 USB 连接的 iPhone。请先连线、信任电脑、解锁手机。"
        )
    if len(devices) > 1:
        rows = "\n".join(f"  - {d.udid}  {d.name}  iOS {d.product_version}" for d in devices)
        raise DemoError(
            "检测到多台 iPhone，请显式传 --udid：\n" + rows
        )
    return devices[0].udid


# ---------------------------------------------------------------------------
# WebDriverAgent Xcode 工程定位
# ---------------------------------------------------------------------------
def resolve_wda_project_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if path.suffix == ".xcodeproj" and path.exists():
        return path
    if path.is_dir():
        candidates = sorted(path.glob("**/WebDriverAgent.xcodeproj"))
        if len(candidates) == 1:
            return candidates[0].resolve()
        if len(candidates) > 1:
            joined = "\n".join(f"  - {p}" for p in candidates)
            raise DemoError(
                "发现多个 WebDriverAgent.xcodeproj，请直接传具体路径：\n" + joined
            )
    raise DemoError(
        f"找不到 WebDriverAgent.xcodeproj：{path}\n"
        "请先 clone 一份 WebDriverAgent 源码，然后把 --project 指到 xcodeproj。"
    )


# ---------------------------------------------------------------------------
# USBMux 端口转发：本机 local_port -> 设备 8100
# ---------------------------------------------------------------------------
class UsbmuxPortForwarder:
    """纯 Python 版 usbmux 转发，避免脚本再依赖 iproxy。"""

    def __init__(self, udid: str, local_port: int, device_port: int = WDA_DEVICE_PORT) -> None:
        self.udid = udid
        self.local_port = local_port
        self.device_port = device_port
        self._listen_sock: Optional[socket.socket] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._stopped = False
        self._upstream_fail_count = 0

    def start(self) -> None:
        if self._listen_sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.local_port))
            sock.listen(8)
        except OSError as exc:
            sock.close()
            raise DemoError(
                f"本地端口监听失败：127.0.0.1:{self.local_port} -> {exc}"
            ) from exc
        self._listen_sock = sock
        self._listen_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name=f"usbmux-fwd-{self.local_port}",
        )
        self._listen_thread.start()
        log(
            f"USBMux 转发已启动：127.0.0.1:{self.local_port} -> device:{self.device_port}"
        )

    def stop(self) -> None:
        self._stopped = True
        if self._listen_sock is not None:
            try:
                self._listen_sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._listen_sock = None

    def _accept_loop(self) -> None:
        listen = self._listen_sock
        if listen is None:
            return
        while not self._stopped:
            try:
                client, _ = listen.accept()
            except OSError:
                break
            try:
                upstream = self._open_upstream()
                self._upstream_fail_count = 0
            except Exception as exc:  # noqa: BLE001
                self._upstream_fail_count += 1
                if self._upstream_fail_count == 1 or self._upstream_fail_count % 20 == 0:
                    log(
                        f"WDA 上游还没连通，累计失败 {self._upstream_fail_count} 次：{exc}"
                    )
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                continue

            threading.Thread(
                target=self._pump, args=(client, upstream), daemon=True
            ).start()
            threading.Thread(
                target=self._pump, args=(upstream, client), daemon=True
            ).start()

    def _open_upstream(self) -> socket.socket:
        from pymobiledevice3 import usbmux

        dev = maybe_sync(usbmux.select_device(udid=self.udid))
        if dev is None:
            raise DemoError(f"设备 {self.udid} 不在 usbmux 设备列表里")
        return maybe_sync(dev.connect(self.device_port))

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(8192)
                if not data:
                    break
                dst.sendall(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass


def pick_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Xcodebuild：真正把 XCTest/WDA 拉起来
# ---------------------------------------------------------------------------
class XcodebuildWdaRunner:
    """后台跑 xcodebuild，并把最近日志保存在内存里便于报错时打印。"""

    def __init__(self, cmd: list[str], cwd: Path) -> None:
        self.cmd = cmd
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._lines: deque[str] = deque(maxlen=80)

    def start(self) -> None:
        if self.proc is not None:
            return
        log("准备启动 xcodebuild，真正把 XCTest/WDA 拉起来")
        log("命令：\n  " + " ".join(self.cmd))
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(
            target=self._drain_output,
            daemon=True,
            name="xcodebuild-log",
        )
        self._thread.start()

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            log("停止 xcodebuild")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.proc = None

    def ensure_alive(self) -> None:
        proc = self.proc
        if proc is None:
            raise DemoError("xcodebuild 还没启动")
        rc = proc.poll()
        if rc is not None:
            tail = "\n".join(self._lines) or "(没有采集到日志)"
            raise DemoError(
                f"xcodebuild 已提前退出，返回码={rc}\n最近日志：\n{tail}"
            )

    def recent_log_tail(self) -> str:
        return "\n".join(self._lines) or "(没有采集到日志)"

    def _drain_output(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            self._lines.append(line)
            print(f"[xcodebuild] {line}", flush=True)


def build_xcodebuild_command(args: argparse.Namespace, project_path: Path, udid: str) -> list[str]:
    ensure_xcodebuild()

    cmd = [
        "xcodebuild",
        "-project",
        str(project_path),
        "-scheme",
        args.scheme,
        "-destination",
        f"id={udid}",
        "-derivedDataPath",
        str(Path(args.derived_data).expanduser().resolve()),
        "-allowProvisioningUpdates",
        "test",
        "CODE_SIGN_STYLE=Automatic",
        "COMPILER_INDEX_STORE_ENABLE=NO",
    ]
    if args.team_id:
        cmd.append(f"DEVELOPMENT_TEAM={args.team_id}")
    if args.bundle_id:
        cmd.append(f"PRODUCT_BUNDLE_IDENTIFIER={args.bundle_id}")
    return cmd


# ---------------------------------------------------------------------------
# 最小版 WDA HTTP client
# ---------------------------------------------------------------------------
@dataclass
class WdaWindowSize:
    width: int
    height: int


class SimpleWdaClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._lock = threading.Lock()

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DemoError(
                f"WDA HTTP 错误 {method} {path}: {exc.code} {detail[:300]}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise DemoError(f"WDA 网络错误 {method} {path}: {exc}") from exc

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            text = raw.decode("utf-8", errors="replace")
            raise DemoError(
                f"WDA 返回了非 JSON 内容 {method} {path}: {text[:300]}"
            ) from exc
        status = data.get("status")
        if status not in (None, 0):
            raise DemoError(
                f"WDA status={status} {method} {path}: {data.get('value')}"
            )
        return data

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._request("GET", "/status").get("value") or {}

    def wait_ready(
        self,
        timeout: float,
        runner: Optional[XcodebuildWdaRunner] = None,
        interval: float = 0.5,
    ) -> None:
        start = time.time()
        last_log = 0.0
        last_error = ""
        while time.time() - start < timeout:
            if runner is not None:
                runner.ensure_alive()
            try:
                value = self.status()
                if value:
                    log(f"WDA /status 已就绪：{json.dumps(value, ensure_ascii=False)}")
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            now = time.time()
            if now - last_log >= 5.0:
                remain = int(timeout - (now - start))
                log(f"等待 WDA 就绪中，剩余约 {remain}s；最近错误：{last_error or '(暂无)'}")
                last_log = now
            time.sleep(interval)
        raise DemoError(f"WDA 在 {timeout}s 内没有就绪：{last_error or '(无更多错误信息)'}")

    def create_session(self) -> str:
        body = {
            "capabilities": {
                "alwaysMatch": {},
                "firstMatch": [{}],
            }
        }
        with self._lock:
            data = self._request("POST", "/session", body)
            value = data.get("value") or {}
            sid = value.get("sessionId") or data.get("sessionId")
            if not sid:
                raise DemoError(f"WDA create session 失败：{data}")
            self.session_id = str(sid)
            log(f"WDA session 已创建：{self.session_id}")
            return self.session_id

    def _ensure_session(self) -> str:
        if self.session_id:
            return self.session_id
        return self.create_session()

    def window_size(self) -> WdaWindowSize:
        sid = self._ensure_session()
        with self._lock:
            data = self._request("GET", f"/session/{sid}/window/size")
        value = data.get("value") or {}
        return WdaWindowSize(
            width=int(value.get("width", 0)),
            height=int(value.get("height", 0)),
        )

    def tap(self, x: float, y: float) -> None:
        sid = self._ensure_session()
        body = {"x": float(x), "y": float(y)}
        with self._lock:
            self._request("POST", f"/session/{sid}/wda/tap/0", body)
        log(f"WDA tap 已发送：({x}, {y})")

    def close(self) -> None:
        if not self.session_id:
            return
        sid = self.session_id
        self.session_id = None
        try:
            self._request("DELETE", f"/session/{sid}")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def print_devices() -> None:
    devices = list_connected_devices()
    if not devices:
        log("没有发现连接中的 iPhone")
        return
    log("发现以下 iPhone：")
    for d in devices:
        print(
            f"  - udid={d.udid}  name={d.name}  model={d.model}  iOS={d.product_version}",
            flush=True,
        )


def keep_alive_until_ctrl_c(runner: Optional[XcodebuildWdaRunner], client: SimpleWdaClient) -> None:
    log("进入 keep-alive 模式；按 Ctrl-C 退出。")
    try:
        while True:
            if runner is not None:
                runner.ensure_alive()
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl-C，准备退出")
    finally:
        client.close()
        if runner is not None:
            runner.stop()


def run_tap_flow(args: argparse.Namespace) -> None:
    require_pymobiledevice3()
    udid = choose_udid(args.udid)
    log(f"目标设备 UDID：{udid}")

    runner: Optional[XcodebuildWdaRunner] = None
    if not args.skip_xcodebuild:
        if not args.project:
            raise DemoError("需要传 --project，指向 WebDriverAgent.xcodeproj")
        project_path = resolve_wda_project_path(args.project)
        log(f"使用 WDA 工程：{project_path}")
        cmd = build_xcodebuild_command(args, project_path, udid)
        runner = XcodebuildWdaRunner(cmd=cmd, cwd=project_path.parent)
        runner.start()
    else:
        log("跳过 xcodebuild，假定手机上的 WDA 已经在跑。")

    local_port = args.local_port or pick_free_local_port()
    forwarder = UsbmuxPortForwarder(udid=udid, local_port=local_port)
    forwarder.start()

    client = SimpleWdaClient(f"http://127.0.0.1:{local_port}", timeout=args.http_timeout)
    try:
        client.wait_ready(timeout=args.wda_timeout, runner=runner)
        client.create_session()
        size = client.window_size()
        log(f"WDA window size（point）：{size.width} x {size.height}")

        x = args.x if args.x is not None else size.width / 2.0
        y = args.y if args.y is not None else size.height / 2.0
        log(f"即将点击坐标（WDA point）：({x}, {y})")
        client.tap(x, y)
        log("iOS 点击流程执行完成。")

        if args.keep_running:
            keep_alive_until_ctrl_c(runner, client)
        else:
            time.sleep(2)
            client.close()
    finally:
        forwarder.stop()
        if runner is not None and not args.keep_running:
            runner.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="单文件版：用 xcodebuild + WDA 控制 iOS 真机点击一次。"
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出当前通过 USB 连着的 iPhone，然后退出。",
    )
    parser.add_argument(
        "--project",
        help="WebDriverAgent.xcodeproj 路径，或包含它的目录。",
    )
    parser.add_argument(
        "--udid",
        help="目标设备 UDID。不传时，如果只连了一台 iPhone，会自动选中。",
    )
    parser.add_argument(
        "--team-id",
        help="Apple Developer Team ID，会传给 xcodebuild 的 DEVELOPMENT_TEAM。",
    )
    parser.add_argument(
        "--bundle-id",
        help=(
            "可选：覆盖 WDA target 的 PRODUCT_BUNDLE_IDENTIFIER，"
            "例如 com.yourname.WebDriverAgentRunner"
        ),
    )
    parser.add_argument(
        "--scheme",
        default="WebDriverAgentRunner",
        help="WDA scheme，默认 WebDriverAgentRunner。",
    )
    parser.add_argument(
        "--derived-data",
        default=str(Path(__file__).resolve().parent / ".wda-derived-data"),
        help="xcodebuild 的 DerivedData 目录。",
    )
    parser.add_argument(
        "--local-port",
        type=int,
        help="本地转发端口；不传则自动分配。",
    )
    parser.add_argument(
        "--x",
        type=float,
        help="点击 X 坐标，单位是 WDA point；不传默认屏幕中心。",
    )
    parser.add_argument(
        "--y",
        type=float,
        help="点击 Y 坐标，单位是 WDA point；不传默认屏幕中心。",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=10.0,
        help="单次 HTTP 请求超时秒数，默认 10。",
    )
    parser.add_argument(
        "--wda-timeout",
        type=float,
        default=180.0,
        help="等待 WDA /status 就绪的超时秒数，默认 180。",
    )
    parser.add_argument(
        "--skip-xcodebuild",
        action="store_true",
        help="跳过 xcodebuild，直接尝试连接已运行的 WDA。",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="点击后保持 xcodebuild/WDA 存活，便于你继续手工调试。",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.list_devices:
            print_devices()
            return 0
        run_tap_flow(args)
        return 0
    except DemoError as exc:
        print("\n[ERROR] " + str(exc), file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print("\n[ERROR] 用户取消", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
