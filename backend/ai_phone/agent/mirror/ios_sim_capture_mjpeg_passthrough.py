"""iOS **虚拟机**镜像通道：WDA MJPEG server → JPEG 直通浏览器。

与真机走同一条 MJPEG 直通路线（方案 §1.10），只有两处技术差异，因此本模块只是
``IosMjpegPassthroughStreamer`` 的一层薄覆盖，切帧 / 尺寸自适应 / settings 下发 /
orientation watcher 全部复用父类。

差异一：**没有 usbmux 端口转发**

```text
真机   iPhone WDA (device:9100) --usbmux--> 127.0.0.1:<随机本地端口> --> httpx
虚拟机 WDA (宿主 127.0.0.1:93xx)  ------------直连------------------> httpx
```

虚拟机与宿主共享网络栈，WDA 的 MJPEG server 本来就监听在宿主回环上（端口由
``SIMCTL_CHILD_MJPEG_SERVER_PORT`` 在 launch 时确定性注入）。真机那层 usbmux 桥
在这里没有任何东西可桥。

差异二：**WdaClient 从虚拟机自己的登记表取**

不能读真机的 ``ios._WDA_CLIENT_MAP``——那张表被真机的拔插生命周期策略和 iOS
readiness probe 消费，混入虚拟机会让两条链路互相干扰（详见
``ios_simulator_driver._SIM_ENDPOINTS`` 的说明）。

端口 fail-closed：拿不到已登记的端点就拒绝推流并说明原因，**不扫描端口、不猜、
不回退真机路径**（方案 §8）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from loguru import logger

from .ios_capture_mjpeg_passthrough import IosMjpegPassthroughStreamer


class IosSimMjpegPassthroughStreamer(IosMjpegPassthroughStreamer):
    """虚拟机版 JPEG 直通推流。"""

    def __init__(
        self,
        serial: str,
        on_jpeg: Callable[[bytes, int, int], None],
        *,
        target_fps: int = 20,
        jpeg_quality: int = 60,
        long_edge: int = 720,
        log_tag: str = "ios-sim-mjpeg-pt",
    ) -> None:
        """
        Args:
            serial: 虚拟机 udid
            on_jpeg: 每帧回调 ``(jpeg_bytes, width, height)``，同父类

        注意这里**没有** ``device_mjpeg_port`` 与 ``wda_local_port_for_settings``
        两个参数：虚拟机的两个端口都由 driver 在启动 WDA 时确定并登记，调用方无从
        指定也不该指定。``start()`` 时按 udid 查登记表。
        """
        super().__init__(
            serial=serial,
            on_jpeg=on_jpeg,
            target_fps=target_fps,
            jpeg_quality=jpeg_quality,
            long_edge=long_edge,
            # 父类的 device_mjpeg_port 是「设备侧端口」，转发用；虚拟机不转发，
            # 真实端口在 _setup_port_forward 里从登记表取。这里给 0 表示未使用。
            device_mjpeg_port=0,
            wda_local_port_for_settings=None,
            log_tag=log_tag,
        )

    # ------------------------------------------------------------------
    def _endpoint(self) -> Optional[Any]:
        try:
            from ai_phone.agent.drivers.ios_simulator_driver import (  # noqa: PLC0415
                get_sim_endpoint,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[{}] 虚拟机驱动模块不可用: {}", self._log_tag, exc)
            return None
        return get_sim_endpoint(self._serial)

    def _get_wda_client(self) -> Any:
        endpoint = self._endpoint()
        return None if endpoint is None else endpoint.client

    def _setup_port_forward(self) -> None:
        """不做转发，直接把 MJPEG 端口指向宿主回环。

        父类在这里起 usbmux 转发并把本地端口写进 ``_mjpeg_local_port``；我们只需
        写端口。父类 ``stop()`` 判 ``_forwarder is not None`` 才清理，保持 ``None``
        即可，无需覆盖 ``stop()``。
        """
        endpoint = self._endpoint()
        if endpoint is None:
            # fail-closed：端点未登记说明 driver 还没就绪。猜端口只会连到别的
            # 实例（端口域连续分配）或空端口，前者更危险——会把另一台的画面推给
            # 这台的观看者。
            raise RuntimeError(
                f"虚拟机 {self._serial} 的 WDA 端点未登记，无法建立镜像；"
                "请确认 driver 已就绪（正常情况下 mirror 启动前会先打开 driver）"
            )
        self._mjpeg_local_port = int(endpoint.mjpeg_port)
        # settings 下发的前置条件是「wda 本地端口已知」，父类用 _wda_local_port
        # 判定；虚拟机的 WDA 端口同样是宿主直连端口。
        self._wda_local_port = int(endpoint.wda_port)
        logger.info(
            "[{}] 虚拟机无需端口转发，直连宿主 udid={} mjpeg=127.0.0.1:{} wda=127.0.0.1:{}",
            self._log_tag, self._serial, self._mjpeg_local_port, self._wda_local_port,
        )


__all__ = ["IosSimMjpegPassthroughStreamer"]
