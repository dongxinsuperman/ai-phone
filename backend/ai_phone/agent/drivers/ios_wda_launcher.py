"""iOS WDA 启动器：``xcodebuild test`` + ``-allowProvisioningUpdates``。

替代原来那条 ``go-ios runwda`` 路径。背景见
``ai-phone/iOS_WDA_Xcode操作手册_2026-04-19.md`` §"这次实际跑通的全过程"——
Xcode/XCTest 官方测试体系是 iOS 17+/26 唯一稳定的 WDA 启动姿势；
``go-ios runwda`` 在 iOS 26 上 100% 撞 XCTest Error 103，不再是可选方案。

本模块只做一件事：
    ``xcodebuild test -project ... -scheme ... -destination 'id=<udid>'
                     -allowProvisioningUpdates``

几个关键决策：

1. **只用 ``test`` 子命令，不走 ``build-for-testing`` + ``test-without-building``
   两段式**。两段式确实更快（首次编译完后 <5s 再起 WDA），但要自己管
   ``*.xctestrun`` 的路径缓存，一旦 Xcode 版本 / 工程配置变动就会踩坑。
   完整 ``xcodebuild test`` 有 Xcode 自己的 incremental build 缓存兜底，
   第二次启动也只要 10~20s，简单优先。

2. **``-allowProvisioningUpdates`` 必加**。免费 Apple ID 签名 7 天过期，
   这个 flag 让 Xcode 每次启动都自动重新走一遍"signing + provisioning"，
   把"证书快过期要重装"这件事彻底变成 agent 启动时的无感副作用。

3. **子进程生命周期 = WDA 生命周期**。``xcodebuild test`` 是阻塞命令，
   进程活着时 XCTest runner 在真机上持续跑；进程死 → WDA 立刻失联。
   我们 spawn 成后台子进程，atexit 钩子统一 kill。

4. **attach 优先**：start() 先探测 WDA 是否已经在跑（HTTP /status），
   如果已经有人（Xcode GUI / 上一次 agent 残留 / 手工 xcodebuild）
   把 WDA 跑起来了，直接复用，不再重复 spawn。避免"Xcode 里开着的
   XCTest 会话被 agent 的 xcodebuild 一脚踢掉"这个体验灾难。
"""
from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx
from loguru import logger


# ``on_status(stage, title, hint, elapsed_ms)``：launcher 在生命周期关键节点
# 通知上层（用于 agent → server → web 实时提示条）。
StatusCallback = Callable[[str, str, str, int], None]

# watcher 从 locked 首次检测起，到触发 respawn 的等待秒数；超过这个值仍 locked
# 说明 Xcode preflight 死锁了（Apple 老 bug，不会自愈）。
_LOCKED_RESPAWN_SEC = 60
# 最多自动 respawn 几次；超过阈值就进入 error 态让人工介入，避免 xcodebuild 无限
# 自循环把 iPhone 的 USB/lockdown/deviceprep 状态搅得更乱。
_MAX_RESPAWN = 2


# ---------------------------------------------------------------------------
# 全局子进程表 + atexit 清理
# ---------------------------------------------------------------------------
_PROCS: Dict[str, subprocess.Popen] = {}
_PROCS_LOCK = threading.Lock()


def _kill_all_wda_procs() -> None:
    """agent 退出时杀掉所有 xcodebuild test 子进程。

    不 kill 的话 XCTest 会在真机上残留 session，下次启动会报
    ``Testing failed ... A session already exists``。
    """
    with _PROCS_LOCK:
        procs = list(_PROCS.items())
        _PROCS.clear()
    for udid, proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
                logger.info("udid={} 终止 xcodebuild test 子进程 pid={}", udid, proc.pid)
            except Exception:  # noqa: BLE001
                pass


atexit.register(_kill_all_wda_procs)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _probe_wda_http(port: int, timeout_s: float = 1.0) -> bool:
    """用短超时 HTTP 打 ``/status``，看 WDA 是不是已经在这个端口活着。

    用于两种场景：
      1. start() 前判断"能不能 attach"
      2. wait_ready 前快速 smoke test
    """
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/status", timeout=timeout_s)
    except Exception:  # noqa: BLE001
        return False
    if resp.status_code != 200:
        return False
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return False
    # WDA /status 成功时字段里会有 `ready` / `state` / `build` 之一
    v = payload.get("value") if isinstance(payload, dict) else None
    return bool(v)


def _port_bind_available(port: int) -> bool:
    """快速试 bind 一下目标端口，判断是不是空闲。用完立刻释放。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
    return True


def _find_xcodebuild() -> Optional[str]:
    """查找 ``xcodebuild``。优先 ``xcode-select -p`` 指向的 Xcode；
    兜底走 ``shutil.which``（可能是 CLT 的，功能不全但存在）。"""
    try:
        p = subprocess.run(
            ["xcode-select", "-p"],
            capture_output=True, text=True, check=False, timeout=3,
        )
        developer_dir = (p.stdout or "").strip()
        if developer_dir:
            candidate = Path(developer_dir) / "usr" / "bin" / "xcodebuild"
            if candidate.exists():
                return str(candidate)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("xcodebuild")


# ---------------------------------------------------------------------------
# IosWdaXcodeLauncher
# ---------------------------------------------------------------------------
class IosWdaXcodeLauncher:
    """用 ``xcodebuild test`` 拉起并守护 WDA。

    典型用法（在 ``open_ios_driver`` 里）::

        launcher = IosWdaXcodeLauncher(
            udid=udid,
            project_dir="/Users/<你>/code/ai-phone/third_party/WebDriverAgent",
            scheme="WebDriverAgentRunner",
            device_port=8100,
            bundle_id="com.<你>.wda",   # 可选，覆盖 .pbxproj 里 PRODUCT_BUNDLE_IDENTIFIER
            team_id="ABC123XYZ",        # 可选，覆盖 .pbxproj 里 DEVELOPMENT_TEAM
        )
        mode = launcher.start()  # 'attach' / 'spawn' / 'disabled'
        # ... wda_client.wait_ready ...
        # 进程结束后 launcher.stop()

    启动策略：
      - 目标端口已有 WDA 响应 → ``mode='attach'``，不 spawn
      - ``project_dir`` 为空/不存在 → ``mode='disabled'``，要求用户自己拉 WDA
      - 其余情况 → ``mode='spawn'``，``xcodebuild test`` 挂到后台

    **注意**：本类不做端口转发（那是 ``_UsbmuxPortForwarder`` 的事）。
    ``device_port`` 这里只用来做"WDA 已经在跑了吗"的 HTTP 探测，
    探测目标是 ``127.0.0.1:<device_port>``——调用方要保证转发已起好，
    或者用户自己在跑 ``iproxy``。
    """

    def __init__(
        self,
        udid: str,
        project_dir: Optional[Path],
        scheme: str = "WebDriverAgentRunner",
        local_probe_port: int = 8100,
        on_status: Optional[StatusCallback] = None,
        bundle_id: Optional[str] = None,
        team_id: Optional[str] = None,
        *,
        # 生命周期策略开关：默认 True 与本字段引入前完全等价（auto 行为）。
        # 调用方按 ``IosWdaLifecyclePolicy`` 传值——launcher 本身不感知 auto/stable
        # 业务语义，只读布尔。两个开关分别对应：
        #   - runtime_drop：xcodebuild test 已起 + WDA 曾 ready，运行中 XPC 失联
        #     退出后是否自动 respawn；
        #   - preflight_deadlock：iPhone 锁屏 ≥ _LOCKED_RESPAWN_SEC 时是否自动
        #     kill + spawn 一次（软拔插）。
        # 详见 docs-internal/iOS_WDA_生命周期策略方案_2026-05-11.md §7.2 / §7.0.1#1。
        allow_runtime_drop_respawn: bool = True,
        allow_preflight_deadlock_respawn: bool = True,
    ) -> None:
        self.udid = udid
        self.project_dir = Path(project_dir).expanduser().resolve() if project_dir else None
        self.scheme = scheme
        self.local_probe_port = local_probe_port
        self._on_status = on_status
        # 这两个空串/None 都视为"不覆盖 .pbxproj"，把判断收敛到一处
        self.bundle_id = bundle_id.strip() if bundle_id and bundle_id.strip() else None
        self.team_id = team_id.strip() if team_id and team_id.strip() else None
        self._allow_runtime_drop_respawn = bool(allow_runtime_drop_respawn)
        self._allow_preflight_deadlock_respawn = bool(allow_preflight_deadlock_respawn)
        self._proc: Optional[subprocess.Popen] = None
        self._log_thread: Optional[threading.Thread] = None
        self._owned = False  # True=本实例 spawn 的，stop 时要 kill
        # locked watcher 状态：xcodebuild 一旦吐出 "Unlock ... to Continue" 就进入
        # 死锁模式（Apple 老 bug：即使 iPhone 后续解锁了，preflight 也不会自恢复）。
        # watcher 每 10s 提示一次 + 60s 后触发自动 respawn，让 agent 完全自愈。
        self._locked_since_ts: Optional[float] = None
        self._locked_watcher: Optional[threading.Thread] = None
        self._locked_stop = threading.Event()
        # respawn 计数：超过 _MAX_RESPAWN 就停止自动重试，让 web 显示 error，
        # 避免 xcodebuild 死循环把 iPhone 的 deviceprep 状态持续搞坏。
        # 注意：preflight 死锁 + 运行中失联（CoreDevice XPC invalidated）共用同一个计数器，
        # 避免两种症状交替触发导致无限重启。
        self._respawn_count = 0
        # 外部 stop() 进来时置 True，让 _drain_output 的自愈分支知道"是我们主动杀的、
        # 别再拉起来"，避免 agent 正常退出时还多起一条 xcodebuild 残留到 iPhone 里。
        self._stopping = False
        self._stage_start_ts: float = time.monotonic()  # 当前 stage 起点，用于 elapsed_ms

    # ------------------------------------------------------------------
    def _emit(self, stage: str, title: str, hint: str = "") -> None:
        """调用上层 on_status 回调；任何异常都吞掉，绝不影响主路径。"""
        now = time.monotonic()
        # 切换 stage 时重置起点；相同 stage 连续上报则累计耗时
        # 这里简单粗暴：每次 _emit 都以上一个 stage 的起点算 elapsed_ms
        elapsed_ms = int((now - self._stage_start_ts) * 1000)
        self._stage_start_ts = now
        cb = self._on_status
        if cb is None:
            return
        try:
            cb(stage, title, hint, elapsed_ms)
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_status 回调异常 udid={} stage={}: {}", self.udid, stage, exc)

    # ------------------------------------------------------------------
    def project_path(self) -> Optional[Path]:
        """返回 ``WebDriverAgent.xcodeproj`` 完整路径。``project_dir`` 下就该有这个。"""
        if self.project_dir is None:
            return None
        p = self.project_dir / "WebDriverAgent.xcodeproj"
        return p if p.exists() else None

    def precheck(self) -> Optional[str]:
        """启动前环境自检，返回错误串或 None（一切就绪）。

        这几项是经验值——在 runbook 里全都亲自踩过一次：
          1. xcodebuild 可执行存在
          2. WebDriverAgent.xcodeproj 存在
          3. project.pbxproj 能读到（签名元信息在里头）
        """
        if self.project_dir is None:
            return "WDA 项目目录未配置（AI_PHONE_WDA_PROJECT_DIR 为空）"
        xb = _find_xcodebuild()
        if xb is None:
            return "没找到 xcodebuild；请先安装 Xcode 并 `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`"
        if self.project_path() is None:
            return f"WebDriverAgent.xcodeproj 不存在于 {self.project_dir}"
        pbx = self.project_dir / "WebDriverAgent.xcodeproj" / "project.pbxproj"
        if not pbx.exists():
            return f"project.pbxproj 丢失：{pbx}"
        return None

    # ------------------------------------------------------------------
    def start(self, *, allow_spawn: bool = True) -> str:
        """返回:
          * ``'attach'``  目标端口已有 WDA，直接复用
          * ``'spawn'``   本实例 spawn 了一个 xcodebuild test
          * ``'disabled'`` 没配置 project_dir（或 precheck 失败 / allow_spawn=False
            且既不能 attach、又没在其它流程的 spawn 上 follow），调用方自己想办法

        **spawn 成功只代表子进程已启动**，不代表 WDA 已 ready；上层必须接着
        跑 ``WdaClient.wait_ready`` 轮询 /status。

        ``allow_spawn=False`` 用于 stable 模式 §7.5.1 状态机：本次"USB 插入会话"
        内已经 spawn 过 / 严格 attach-only 子方案下，外层禁止再走 ``xcodebuild
        test``。**attach / 复用已有 xcodebuild 子进程都不受 allow_spawn 影响**——
        语义只是"是否允许新起一条 xcodebuild test"。
        """
        self._emit("initializing", "iOS 启动中", "正在检测 WDA 状态…")

        # 1. attach 优先（不受 allow_spawn 影响）
        if _probe_wda_http(self.local_probe_port, timeout_s=0.8):
            logger.info(
                "udid={} 检测到 127.0.0.1:{} 已有 WDA 响应 → attach，不启动 xcodebuild test",
                self.udid, self.local_probe_port,
            )
            # attach 模式下直接可用，但 "真正 ready" 还要 wait_ready 确认，所以
            # 这里仍保持 compiling 状态（实际瞬间就会被 mark_ready 刷成 ready）。
            self._emit("compiling", "WDA 已在运行", "复用现有 WDA 连接…")
            return "attach"

        err = self.precheck()
        if err is not None:
            port = self.local_probe_port
            logger.warning(
                "udid={} WDA 自动启动已禁用：{}\n"
                "  → 过渡方案：在 Xcode 里打开 {}/WebDriverAgent.xcodeproj"
                " → 选设备 → Cmd+U（Product → Test）\n"
                "  → 并在另一个终端跑 `iproxy {}:{}`（让 agent 能连到 WDA）\n"
                "  → 或配置 AI_PHONE_WDA_PROJECT_DIR 指向 WDA 工程目录",
                self.udid, err, self.project_dir or "<未配置>", port, port,
            )
            self._emit("error", "WDA 自动启动已禁用", err)
            return "disabled"

        with _PROCS_LOCK:
            existing = _PROCS.get(self.udid)
            if existing is not None and existing.poll() is None:
                logger.info(
                    "udid={} xcodebuild test 已在跑 pid={}，跳过启动",
                    self.udid, existing.pid,
                )
                self._proc = existing
                self._owned = False
                self._emit("compiling", "WDA 正在编译", "xcodebuild 已在其它流程中启动，请等待…")
                return "spawn"

        # 真要新起 xcodebuild 之前看 allow_spawn —— attach / 复用都不会走到这里。
        if not allow_spawn:
            logger.warning(
                "udid={} 当前策略拒绝 spawn xcodebuild test（attach 失败 + allow_spawn=False）。"
                "可能原因：stable 模式本次 USB 插入会话内已 spawn 过，或严格 attach-only 子方案",
                self.udid,
            )
            self._emit(
                "error",
                "WDA 未就绪，未自动重启",
                "当前策略禁止 agent 主动启动 xcodebuild。请人工确认 WDA 是否仍在运行 / "
                "USB 是否稳定 / iPhone 是否解锁；如需重启，请拔出 USB 并重新插入设备。",
            )
            return "disabled"

        proc = self._spawn_xcodebuild()
        if proc is None:
            self._emit("error", "xcodebuild 启动失败", "请查看 agent 日志排查 Xcode 环境")
            return "disabled"
        self._proc = proc
        self._owned = True
        self._emit(
            "compiling",
            "WDA 编译中",
            "首次冷启动约 1-3 分钟。请**保持 iPhone 解锁状态**直到屏幕出现 Automation Running。",
        )
        return "spawn"

    # ------------------------------------------------------------------
    def _spawn_xcodebuild(self) -> Optional[subprocess.Popen]:
        """真正执行 ``xcodebuild test`` + 起 drain_output 线程。

        拆出来是为了 ``_respawn_once`` 能复用（locked 死锁 60s 后自动重启一次
        xcodebuild，相当于 Agent 替你做了一次"USB 热拔插"）。
        """
        cmd = self._build_cmd()
        logger.info(
            "udid={} 启动 xcodebuild test：\n  cwd={}\n  cmd={}",
            self.udid, self.project_dir, " ".join(cmd),
        )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                preexec_fn=os.setsid if os.name == "posix" else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("udid={} xcodebuild test spawn 失败：{}", self.udid, exc)
            return None
        with _PROCS_LOCK:
            _PROCS[self.udid] = proc
        logger.info("udid={} xcodebuild test 已 spawn pid={}", self.udid, proc.pid)

        # 每次 spawn 都要起一个新的 drain 线程；老的会在老 proc EOF 时自动退出
        t = threading.Thread(
            target=self._drain_output,
            args=(proc,),
            daemon=True,
            name=f"wda-xcodebuild-{self.udid[:8]}-p{proc.pid}",
        )
        t.start()
        self._log_thread = t
        return proc

    # ------------------------------------------------------------------
    def _respawn_once(self, reason: str = "preflight_deadlock") -> None:
        """WDA 进程失效时的自救：kill 当前 xcodebuild → 等 2s → 重新 spawn。

        这就相当于 Agent 替用户做了一次 USB 热拔插的"软复位"。目前会触发这条
        路径的场景有两种：

          * ``preflight_deadlock``：iPhone 锁屏超过 ``_LOCKED_RESPAWN_SEC``，
            Xcode preflight 状态机死锁（Apple 老 bug，单向的）。
          * ``runtime_drop``：``xcodebuild test`` 已经跑起来、WDA 曾经 ready，
            但运行过程中 CoreDevice XPC 通道被系统 invalidate，xcodebuild
            进程退出（iOS 17+/26 常见，诱因包括锁屏被回收、USB tunnel reset
            等，观察不到也复现不了，基本只能靠重启）。
        """
        if self._stopping:
            logger.debug("udid={} stop 进行中，跳过 respawn(reason={})", self.udid, reason)
            return
        if self._respawn_count >= _MAX_RESPAWN:
            logger.warning(
                "udid={} 自动 respawn 已达上限 {} 次，不再重试；请人工确认 iPhone 是否已解锁 / 信任证书是否有效",
                self.udid, _MAX_RESPAWN,
            )
            self._emit(
                "error",
                "自动恢复失败",
                f"已自动重启 WDA {_MAX_RESPAWN} 次仍无法就绪。"
                "请确认：iPhone 已解锁进入主屏幕 / 已信任开发者证书 / USB 线正常。"
                "之后点击「重启 Agent」或拔插 USB 重试。",
            )
            return
        self._respawn_count += 1
        if reason == "runtime_drop":
            logger.warning(
                "udid={} 运行中 WDA 失联（CoreDevice XPC invalidated），"
                "自动 respawn xcodebuild（第 {} 次 / 最多 {} 次）",
                self.udid, self._respawn_count, _MAX_RESPAWN,
            )
            self._emit(
                "runtime_drop",
                "WDA 运行中掉线，正在自动重启",
                f"xcodebuild ↔ iPhone 的 XPC 通道被系统回收（Apple 老 bug，iOS 17+/26 常见），"
                f"设备未拔插但 WDA 已死。第 {self._respawn_count}/{_MAX_RESPAWN} 次自动重启中…",
            )
        else:
            logger.warning(
                "udid={} preflight 死锁，自动 respawn xcodebuild（第 {} 次 / 最多 {} 次）",
                self.udid, self._respawn_count, _MAX_RESPAWN,
            )
            self._emit(
                "preflight_deadlock",
                "xcodebuild 卡住，正在自动重启",
                f"Xcode preflight 死锁不会自恢复（Apple 老 bug）。第 {self._respawn_count}/{_MAX_RESPAWN} 次自动重启中…",
            )

        # kill 当前 proc + 等 iPhone 侧 deviceprep 服务清理
        old = self._proc
        if old is not None and old.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(old.pid), 9)
                else:
                    old.kill()
            except Exception as exc:  # noqa: BLE001
                logger.debug("kill 老 xcodebuild 失败：{}", exc)
        time.sleep(2.0)

        # 重新 spawn；watcher 自己会被 _drain_output 在下次 locked 时再触发起来
        self._locked_since_ts = None
        new_proc = self._spawn_xcodebuild()
        if new_proc is None:
            self._emit("error", "重启 WDA 失败", "xcodebuild 进程创建失败，请查看 agent 日志")
            return
        self._proc = new_proc
        self._owned = True
        self._emit(
            "compiling",
            "已重启 WDA，重新编译中",
            "第二次启动通常 10-20s 即可就绪（复用增量编译缓存）。",
        )

    # ------------------------------------------------------------------
    def _build_cmd(self) -> List[str]:
        """组装 ``xcodebuild test`` 命令。

        几个参数的取舍：
          * ``-destination 'id=<udid>'`` 比 ``'platform=iOS,name=<name>'`` 稳，
            iPhone 设备名字里带空格/中文时后者会踩 parser bug
          * ``-allowProvisioningUpdates`` 详见模块 docstring
          * ``COMPILER_INDEX_STORE_ENABLE=NO`` 跳过 index store 写，
            CI/agent 场景下没人用 Xcode 索引这份产物，单纯浪费磁盘
          * 不加 ``-quiet``——WDA 日志里的签名错误 / Info.plist 缺字段是踩坑
            头号线索，必须流到 logger 里
          * ``PRODUCT_BUNDLE_IDENTIFIER`` / ``DEVELOPMENT_TEAM``：可选 build settings
            override，让 .pbxproj 在 git 上保持"通用模板"，每台 Mac 用 .env 注入
            自己的 Bundle Id / Team Id（背景见模块顶部 docstring）
        """
        xb = _find_xcodebuild() or "xcodebuild"
        cmd = [
            xb, "test",
            "-project", "WebDriverAgent.xcodeproj",
            "-scheme", self.scheme,
            "-destination", f"id={self.udid}",
            "-allowProvisioningUpdates",
            "COMPILER_INDEX_STORE_ENABLE=NO",
        ]
        # build settings override：放在 -args 之后是 xcodebuild 标准用法（key=value 形式）
        if self.bundle_id:
            cmd.append(f"PRODUCT_BUNDLE_IDENTIFIER={self.bundle_id}")
        if self.team_id:
            cmd.append(f"DEVELOPMENT_TEAM={self.team_id}")
        return cmd

    # ------------------------------------------------------------------
    def _drain_output(self, proc: Optional[subprocess.Popen] = None) -> None:
        """把 xcodebuild 输出流式读到 logger，顺便识别常见错误 pattern
        并贴人话建议。

        ``proc`` 可选——调用方传具体的 Popen 实例，支持 respawn 之后对新进程
        起一个新的 drain 线程。为了向后兼容，不传就用 ``self._proc``。
        """
        if proc is None:
            proc = self._proc
        if proc is None or proc.stdout is None:
            return
        hints: Dict[str, bool] = {
            "signing": False,
            "bundle_dup": False,
            "privacy": False,
            "xctest_103": False,
            "test_started": False,
        }
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                low = line.lower()
                # 几个判断关键字
                if "code sign" in low or "provisioning profile" in low or "requires a development team" in low:
                    hints["signing"] = True
                if "duplicate bundle identifier" in low or "bundle identifier" in low and "not available" in low:
                    hints["bundle_dup"] = True
                if "nslocation" in low and "usage description" in low:
                    hints["privacy"] = True
                if "error code: 103" in low or "xctesterrordomain" in low:
                    hints["xctest_103"] = True
                if "test suite" in low and "started" in low:
                    if not hints["test_started"]:
                        # 第一次 Test Suite started = WDA runner 已经在真机启动，
                        # HTTP server 也就马上起来了。不直接 emit ready（要等
                        # wait_ready 真正连通才算），但 UI 上可以切到"收尾中"。
                        self._emit(
                            "compiling",
                            "WDA 即将就绪",
                            "XCTest 已在 iPhone 上启动，正在等待 WDA HTTP 服务响应…",
                        )
                    hints["test_started"] = True

                # 锁屏/未解锁：Xcode 会卡在 "Run Destination Preflight: The destination
                # is not ready."，错误码 `com.apple.dt.deviceprep Code=-3` / 文案
                # "Unlock xxx to Continue" 或 "device is locked"。
                # **Apple 老 bug**：进入这个 preflight waiting 之后，即使 iPhone
                # 后续解锁了，xcodebuild 也不会自恢复，只能 Ctrl+C 重启或拔插 USB。
                # 所以触发后要启动一个 watcher 线程，每 10s 重复 WARNING，并在
                # 60s 后明确建议用户重启——不要让提示被 DEBUG 日志淹没。
                if (
                    "unlock" in low and "to continue" in low
                ) or "device is locked" in low or "com.apple.dt.deviceprep code=-3" in low:
                    if not hints.get("locked"):
                        hints["locked"] = True
                        self._start_locked_watcher()

                # 分级：警告级日志走 WARNING 以便 logger 过滤也能看到
                if low.startswith("error") or " error:" in low or '"level":"error"' in low:
                    logger.warning("[wda-xcb:{}] {}", self.udid[:8], line)
                else:
                    logger.debug("[wda-xcb:{}] {}", self.udid[:8], line)
        except Exception as exc:  # noqa: BLE001
            logger.debug("wda-xcb 日志线程异常：{}", exc)
        finally:
            rc = proc.poll()
            with _PROCS_LOCK:
                cur = _PROCS.get(self.udid)
                if cur is proc:
                    _PROCS.pop(self.udid, None)

            # 退出 hint 分类
            if rc == 0 or hints["test_started"]:
                logger.info("udid={} xcodebuild test 正常结束 rc={}", self.udid, rc)
                # 只要 test_started 过（= WDA 曾经在 iPhone 上跑起来），xcodebuild 再退出
                # 就意味着运行中失联——不是签名/配置问题，走 runtime_drop 自愈。
                # 另外如果此时 stop() 已经在执行（self._stopping=True），_respawn_once
                # 里会自己吞掉，不会重复拉起。
                if hints["test_started"]:
                    # 策略短路（§7.0.1 #1）：stable 下禁止 runtime_drop 自动 respawn，
                    # 只发 device_status 让浏览器提示"未自动重启，请人工拔插"。
                    # auto 默认 True，与本短路引入前完全等价。_respawn_once 本体不动。
                    if not self._allow_runtime_drop_respawn:
                        logger.warning(
                            "udid={} iOS WDA lifecycle 禁止 runtime_drop 自动 respawn，"
                            "请人工确认设备并按需重新插入 USB",
                            self.udid,
                        )
                        # stage 用 error（DeviceStage Literal 已定义、前端有红色卡片）。
                        # 上方 _respawn_once 那处沿用 runtime_drop 是 auto 路径"正在重启
                        # 1/2"的过渡态、性质不同且 §7.0.1 #11 明确不动；本短路是 stable
                        # 终态失败，必须走 error 才能保证浏览器真正显示提示（修 P2-2）。
                        self._emit(
                            "error",
                            "WDA 运行中掉线，未自动重启",
                            "当前 iOS WDA 生命周期策略为 stable：xcodebuild ↔ iPhone 的 XPC "
                            "通道被系统回收，但 agent 不主动重启 WDA。请人工确认 iPhone 已解锁、"
                            "WDA 是否仍在运行；若需恢复，请拔出 USB 并重新插入设备走一遍人工准备。",
                        )
                        return
                    threading.Thread(
                        target=self._respawn_once,
                        kwargs={"reason": "runtime_drop"},
                        daemon=True,
                        name=f"wda-respawn-runtime-{self.udid[:8]}",
                    ).start()
                return

            tips: List[str] = []
            if hints["signing"]:
                tips.append(
                    "签名问题：请在 Xcode 里打开工程，TARGETS → WebDriverAgentRunner"
                    " → Signing & Capabilities：选 Personal Team / 打开 Automatically"
                    " manage signing / 把 Bundle Identifier 改成唯一值（例如 com.<你>.wda）"
                )
            if hints["bundle_dup"]:
                tips.append(
                    "Bundle Identifier 冲突/不可用：Personal Team 不能注册常见 id，"
                    "改成 com.<自定义>.wda，并在 Build Settings → Product Bundle Identifier"
                    " 的 Any iOS SDK 行也一起改"
                )
            if hints["privacy"]:
                tips.append(
                    "Info.plist 隐私字段缺失：TARGETS → WebDriverAgentRunner → Info 里补齐"
                    " NSLocationWhenInUseUsageDescription / NSLocationAlwaysUsageDescription"
                    " / NSLocationAlwaysAndWhenInUseUsageDescription 三个 key 的非空字符串"
                )
            if hints["xctest_103"]:
                tips.append(
                    "XCTest Error 103：一般是 Xcode 版本比 iOS 老。确保已切换到完整 Xcode"
                    "（`sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`）并升级到能匹配 iOS 版本的 Xcode"
                )
            if not tips:
                tips.append(
                    "请手动跑一次 `xcodebuild test -project WebDriverAgent.xcodeproj"
                    f" -scheme {self.scheme} -destination 'id={self.udid}'"
                    " -allowProvisioningUpdates` 看完整报错"
                )
            logger.warning(
                "udid={} xcodebuild test 异常退出 rc={}\n  → {}",
                self.udid, rc, "\n  → ".join(tips),
            )

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """kill 本实例 spawn 的 xcodebuild；attach 模式下是 no-op。"""
        # 先置 stopping 标记：_drain_output 在 xcodebuild 进程退出后会看这个标记，
        # True 就不再触发 runtime_drop 自愈，避免 agent 关停时还留一条残余的
        # xcodebuild 子进程挂在 iPhone 上。
        self._stopping = True
        # 任何 stop 都顺手关 watcher，避免线程泄漏
        if self._locked_stop is not None:
            self._locked_stop.set()
        if not self._owned:
            self._proc = None
            return
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            # process group kill（xcodebuild 下会 fork 一堆子进程：swift-frontend / xctest 等）
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
                except Exception:  # noqa: BLE001
                    proc.terminate()
            else:
                proc.terminate()
            logger.info("udid={} 已终止 xcodebuild test pid={}", self.udid, proc.pid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stop xcodebuild 异常 udid={}: {}", self.udid, exc)
        # 给子进程 2s 清理；不退就硬 kill
        try:
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), 9)
                else:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
        with _PROCS_LOCK:
            _PROCS.pop(self.udid, None)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    # locked watcher
    # ------------------------------------------------------------------
    def _start_locked_watcher(self) -> None:
        """xcodebuild 报 locked 的第一次立刻调一次。后续重复的 locked 行忽略。

        watcher 行为：
          * 0s：打首条 WARNING（面容/密码解锁 + 进入主屏幕）
          * 之后每 10s 再打一次，并累计显示"已等 Xs"
          * 60s 未恢复：升级 WARNING，明确告诉用户这是 Xcode preflight 死锁，
            **不会自愈**，请 Ctrl+C 重启 agent 或 USB 热拔插
          * 进程退出 / mark_ready() 被调用 / stop() 被调用 → watcher 自动停
        """
        if self._locked_watcher is not None and self._locked_watcher.is_alive():
            return
        self._locked_since_ts = time.monotonic()
        self._locked_stop.clear()

        udid_short = self.udid[:8]
        logger.warning(
            "[wda-xcb:{}] iPhone 锁屏中，xcodebuild 已停在 preflight。"
            "**请面容/密码解锁 + 滑到主屏幕**，并临时关掉自动锁屏"
            "（设置 → 显示与亮度 → 自动锁定 → 永不）。",
            udid_short,
        )
        # 0~60s 阶段的 web 提示也要按 lifecycle 策略分两套——auto 下承诺 60s 后
        # 自动重启 WDA；stable 下根本不会自动重启，再刷"X s 后自动重启"会误导
        # 现场运维（修 P2-1 / Codex 审查）。下面的 watcher loop 同理。
        if self._allow_preflight_deadlock_respawn:
            initial_hint = (
                "面容/密码解锁 + 滑动到主屏幕。\n"
                f"若超过 {_LOCKED_RESPAWN_SEC}s 未解锁，Agent 会自动重启 WDA 一次（相当于软拔插）。"
            )
        else:
            initial_hint = (
                "面容/密码解锁 + 滑动到主屏幕。\n"
                "当前策略 Agent 不会自动重启 WDA；若长时间未恢复，请拔出 USB 重新插入设备。"
            )
        self._emit("need_unlock", "请解锁 iPhone", initial_hint)

        def _loop() -> None:
            respawn_triggered = False
            while not self._locked_stop.wait(10.0):
                proc = self._proc
                if proc is None or proc.poll() is not None:
                    return
                elapsed = int(time.monotonic() - (self._locked_since_ts or time.monotonic()))
                # 10s 节拍：日志 + web 提示条都滚一次
                logger.warning(
                    "[wda-xcb:{}] iPhone 仍锁屏中，已等 {}s —— 请立刻解锁到主屏幕。",
                    udid_short, elapsed,
                )
                if self._allow_preflight_deadlock_respawn:
                    rolling_hint = (
                        f"已等待 {elapsed}s —— 请面容/密码解锁并滑到主屏幕。\n"
                        f"{max(0, _LOCKED_RESPAWN_SEC - elapsed)}s 后 Agent 将自动重启 WDA。"
                    )
                else:
                    rolling_hint = (
                        f"已等待 {elapsed}s —— 请面容/密码解锁并滑到主屏幕。\n"
                        "当前策略 Agent 不会自动重启 WDA；若长时间未恢复，请拔出 USB 重新插入设备。"
                    )
                self._emit("need_unlock", "请解锁 iPhone", rolling_hint)
                if elapsed >= _LOCKED_RESPAWN_SEC and not respawn_triggered:
                    respawn_triggered = True
                    # 策略短路（§7.0.1 #1）：stable 下禁止 preflight_deadlock 自动
                    # respawn——iPhone 锁屏即使到了 60s 阈值也只刷 need_unlock 提示，
                    # 让用户自己解锁。auto 默认 True，与本短路引入前完全等价。
                    if not self._allow_preflight_deadlock_respawn:
                        logger.warning(
                            "[wda-xcb:{}] iOS WDA lifecycle 禁止 preflight_deadlock 自动 respawn，"
                            "watcher 继续按 10s 节拍刷 need_unlock 提示等待人工解锁",
                            udid_short,
                        )
                        # watcher 不退出，沿用上面的 10s 节拍刷新 need_unlock 提示。
                        # respawn_triggered 保持 True，后续 elapsed 增长不再重复打日志；
                        # xcodebuild 被用户解锁后自己推进 / 进程死掉时 watcher 自然退出，
                        # 无线程泄漏。
                        continue
                    # 关掉当前 watcher（会被 _respawn_once 后的下一次 locked 重新触发）
                    self._locked_stop.set()
                    # 关键：在独立线程里做 kill + spawn，避免阻塞 watcher（虽然
                    # 本 watcher 马上要退出，但 kill 老 proc 有 wait，不放心）
                    threading.Thread(
                        target=self._respawn_once,
                        daemon=True,
                        name=f"wda-respawn-{udid_short}",
                    ).start()
                    return

        self._locked_watcher = threading.Thread(
            target=_loop,
            daemon=True,
            name=f"wda-locked-watch-{udid_short}",
        )
        self._locked_watcher.start()

    def mark_ready(self) -> None:
        """wait_ready 成功后由调用方回调，停掉 watcher（避免继续刷提示）。"""
        if self._locked_stop is not None:
            self._locked_stop.set()
        self._locked_since_ts = None
        self._emit("ready", "WDA 已就绪", "设备就绪，可以开始镜像/自动化。")


__all__ = ["IosWdaXcodeLauncher", "_probe_wda_http", "_port_bind_available"]
