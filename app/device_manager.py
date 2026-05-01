"""
Device Manager for QRNG Hardware.

Handles communication with Crypta Labs devices:
- Firefly (PCIe) - detected as /dev/ttyACM*
- QCicada (USB) - detected as /dev/ttyUSB*

Uses official pyqcc library. Two read modes are supported per device:

One-shot mode (streaming_mode: false):
  Each request calls start_one_shot(). Limited to 35,200 bytes per call
  for Firefly and 13,440 bytes for QCicada; larger requests use a temporary
  continuous mode internally.

Streaming mode (streaming_mode: true):
  The device is started in continuous mode at service startup and stays there.
  Each request calls read_continuous() directly — no per-request start/stop,
  no size limit. Optionally, the device can "sleep" (continuous mode stopped)
  after a configurable idle period and wake automatically on the next request.

Post-processing mode is set automatically at startup via /opt/firefly/qcc-cli -P <mode>.
"""

import asyncio
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

try:
    # IMPORTANT: pyqcc.__init__.py doesn't expose cmdctrl at the top level
    # Must use explicit import
    from pyqcc import cmdctrl
    PYQCC_AVAILABLE = True
except ImportError:
    PYQCC_AVAILABLE = False
    logging.warning("pyqcc not available - QRNG devices will not work")

try:
    import pyqcicada
    PYQCICADA_AVAILABLE = True
except ImportError:
    PYQCICADA_AVAILABLE = False
    logging.info("pyqcicada not available - signed read features disabled")

from .config import DeviceConfig, get_config

logger = logging.getLogger(__name__)

QCC_CLI = "/opt/firefly/qcc-cli"


def _flush_input(state: "DeviceState") -> None:
    """Clear the serial receive buffer to remove stale random bytes after stop().

    The Firefly streams ~20 Mbps into the OS buffer from the moment
    start_continuous() is called.  After stop(), bytes remain buffered
    and will corrupt the next command's ACK read unless explicitly cleared.

    pyqcc's comm.flush() only flushes the write buffer; we must reach the
    underlying pyserial object directly.  Wrapped in try/except to be safe
    across pyqcc versions.
    """
    try:
        state.qcc_device._comm._ser.reset_input_buffer()
    except Exception:
        pass


class DeviceStatus(Enum):
    """Device operational status."""
    OFFLINE = "offline"
    ONLINE = "online"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class DeviceState:
    """Runtime state for a device."""
    config: DeviceConfig
    status: DeviceStatus = DeviceStatus.OFFLINE
    qcc_device: Optional[any] = None  # pyqcc device handle
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    bytes_served: int = 0
    requests_served: int = 0
    last_request_time: Optional[float] = None
    error_message: Optional[str] = None
    streaming_active: bool = False  # True if device is currently in continuous mode

    @property
    def is_available(self) -> bool:
        """Check if device is available for requests."""
        return self.status == DeviceStatus.ONLINE and not self.lock.locked()


class DeviceManager:
    """
    Manages QRNG devices using the official pyqcc library.

    Each device has a mutex to ensure bytes are never served to
    multiple requests simultaneously. Requests are routed based on
    primary device assignment with fallback to same-type devices,
    then other types.

    Supports two read modes per device (configured in config.yaml):

    - One-shot mode (default): start_one_shot() per request, ≤35,200 bytes
      for Firefly or ≤13,440 bytes for QCicada; larger requests use a
      temporary continuous-mode burst internally.

    - Streaming mode: device stays in continuous mode permanently;
      reads call read_continuous() with no size limit. An optional idle
      timeout will stop continuous mode after inactivity and restart it
      transparently on the next request (sleep/wake).

    Post-processing mode is set via /opt/firefly/qcc-cli at startup.
    """

    def __init__(self):
        self.devices: dict[str, DeviceState] = {}
        self._initialized = False
        self._idle_monitor_task: Optional[asyncio.Task] = None
        # Dedicated executor for device I/O — separate from the event loop's
        # default executor so asyncio.run()'s shutdown_default_executor() never
        # waits on a stuck device thread.
        self._executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="qrng-device"
        )

    async def initialize(self) -> None:
        """Initialize all configured devices."""
        if not PYQCC_AVAILABLE:
            logger.error("pyqcc library not available - cannot initialize devices")
            logger.error("Install with: pip install pyqcc-x.y.z-py3-none-any.whl")
            return

        config = get_config()

        for dev_config in config.devices:
            if not dev_config.enabled:
                logger.info(f"Device {dev_config.id} is disabled, skipping")
                continue

            state = DeviceState(config=dev_config)
            self.devices[dev_config.id] = state

            try:
                await self._connect_device(dev_config.id)
                if dev_config.streaming_mode:
                    await self._start_streaming(dev_config.id)
            except Exception as e:
                state.status = DeviceStatus.ERROR
                state.error_message = str(e)
                logger.error(f"Failed to initialize device {dev_config.id}: {e}")

        self._initialized = True

        # Launch idle monitor if any streaming device has a sleep timeout configured
        needs_monitor = any(
            s.config.streaming_mode and s.config.streaming_idle_timeout > 0
            for s in self.devices.values()
        )
        if needs_monitor:
            self._idle_monitor_task = asyncio.create_task(self._idle_monitor())
            logger.info("Streaming idle monitor started")

    async def shutdown(self) -> None:
        """Shutdown all devices gracefully."""
        if self._idle_monitor_task:
            self._idle_monitor_task.cancel()
            try:
                await self._idle_monitor_task
            except asyncio.CancelledError:
                pass
            self._idle_monitor_task = None

        for device_id, state in self.devices.items():
            try:
                if state.qcc_device:
                    loop = asyncio.get_running_loop()

                    def _close_device():
                        if state.streaming_active:
                            try:
                                state.qcc_device.stop()
                            except Exception:
                                pass
                        state.streaming_active = False
                        try:
                            state.qcc_device.close_comm()
                        except Exception as e:
                            logger.warning(f"Error closing device {device_id}: {e}")

                    await loop.run_in_executor(self._executor, _close_device)

                state.status = DeviceStatus.OFFLINE
                logger.info(f"Device {device_id} shut down")
            except Exception as e:
                logger.error(f"Error shutting down device {device_id}: {e}")

        self._initialized = False

        # Abandon the executor without waiting — any in-flight device threads
        # (close_comm, one-shot reads) are allowed to finish or be killed on
        # process exit.  cancel_futures drops queued-but-not-started tasks.
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _connect_device(self, device_id: str) -> None:
        """Establish connection to a device using pyqcc."""
        state = self.devices[device_id]
        config = state.config

        # Close existing connection if any
        if state.qcc_device:
            try:
                loop = asyncio.get_running_loop()

                def _close_existing():
                    try:
                        state.qcc_device.close_comm()
                    except:
                        pass

                await loop.run_in_executor(self._executor, _close_existing)
            except Exception as e:
                logger.warning(f"Error closing existing device {device_id}: {e}")

        loop = asyncio.get_running_loop()

        def _create_device():
            """Set post-processing mode then open pyqcc connection."""
            # Set post-processing mode via qcc-cli before opening the port.
            # qcc-cli exits immediately after setting the mode so there is no
            # port conflict with the pyqcc connection opened right after.
            result = subprocess.run(
                [QCC_CLI, "-d", config.path, "-P", str(config.post_processing_mode)],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"qcc-cli -P {config.post_processing_mode} failed for "
                    f"{device_id}: {result.stderr.decode().strip()}"
                )
            logger.info(
                f"Device {device_id} post-processing mode set to "
                f"{config.post_processing_mode}"
            )

            device = cmdctrl.device("serial", config.path)
            return device

        try:
            state.qcc_device = await loop.run_in_executor(self._executor, _create_device)
            state.status = DeviceStatus.ONLINE
            state.error_message = None
            logger.info(f"Device {device_id} connected successfully")
        except Exception as e:
            state.status = DeviceStatus.ERROR
            state.error_message = str(e)
            raise RuntimeError(f"Failed to initialize device {device_id}: {e}")

    async def _start_streaming(self, device_id: str) -> None:
        """Start continuous mode on a device and mark it as streaming."""
        state = self.devices[device_id]
        loop = asyncio.get_running_loop()

        def _do_start():
            _flush_input(state)
            if not state.qcc_device.start_continuous():
                raise RuntimeError("Failed to start continuous mode")

        await loop.run_in_executor(self._executor, _do_start)
        state.streaming_active = True
        logger.info(f"Device {device_id} continuous mode started")

    async def _idle_monitor(self) -> None:
        """
        Background task: stop continuous mode on streaming devices that have
        been idle longer than their configured streaming_idle_timeout.

        Checks every 30 seconds. Acquires the device lock before stopping to
        avoid racing with in-flight requests. Skips devices whose lock is
        currently held rather than blocking.
        """
        CHECK_INTERVAL = 30  # seconds
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            for device_id, state in self.devices.items():
                if not state.config.streaming_mode or not state.streaming_active:
                    continue
                timeout_secs = state.config.streaming_idle_timeout * 60
                if timeout_secs <= 0:
                    continue
                idle_secs = time.time() - (state.last_request_time or 0)
                if idle_secs < timeout_secs:
                    continue
                # Don't block the monitor on a busy device — retry next cycle
                if state.lock.locked():
                    continue
                async with state.lock:
                    state.status = DeviceStatus.BUSY
                    try:
                        loop = asyncio.get_running_loop()

                        def _do_stop():
                            try:
                                state.qcc_device.stop()
                            except RuntimeError:
                                # stop() command was sent and received by the device;
                                # the ACK read failed due to buffered random bytes —
                                # treat as a successful stop.
                                pass
                            _flush_input(state)

                        await loop.run_in_executor(self._executor, _do_stop)
                        logger.info(
                            f"Device {device_id} entering sleep mode "
                            f"after {idle_secs:.0f}s idle"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Device {device_id} sleep transition error: {e}"
                        )
                    finally:
                        state.streaming_active = False  # always — device IS stopped
                        if state.status == DeviceStatus.BUSY:
                            state.status = DeviceStatus.ONLINE

    def get_device_ids_by_type(self, device_type: str) -> list[str]:
        """Get list of device IDs for a given type."""
        return [
            dev_id for dev_id, state in self.devices.items()
            if state.config.type == device_type
        ]

    def get_fallback_order(self, primary_device_id: str) -> list[str]:
        """
        Get ordered list of devices for fallback routing.

        Order: primary -> same type -> other type
        """
        if primary_device_id not in self.devices:
            return list(self.devices.keys())

        primary_type = self.devices[primary_device_id].config.type

        # Start with primary
        order = [primary_device_id]

        # Add other devices of same type
        for dev_id in self.devices:
            if dev_id != primary_device_id:
                if self.devices[dev_id].config.type == primary_type:
                    order.append(dev_id)

        # Add devices of other types
        for dev_id in self.devices:
            if dev_id not in order:
                order.append(dev_id)

        return order

    async def read_bytes(
        self,
        primary_device_id: str,
        num_bytes: int,
        timeout: float = 5.0
    ) -> tuple[bytes, str]:
        """
        Read bytes from device with optional fallback routing.

        Args:
            primary_device_id: Preferred device to read from
            num_bytes: Number of bytes to read
            timeout: Maximum time to wait for an available device

        Returns:
            Tuple of (bytes read, device_id that served the request)

        Raises:
            TimeoutError: If no device available within timeout
            RuntimeError: If device read fails
        """
        config = get_config()

        # Determine device list based on failover setting
        if config.service.failover_enabled:
            # Failover enabled: try primary, then similar devices, then others
            fallback_order = self.get_fallback_order(primary_device_id)
        else:
            # Failover disabled: only use primary device
            fallback_order = [primary_device_id]

        start_time = time.monotonic()

        while (time.monotonic() - start_time) < timeout:
            for device_id in fallback_order:
                state = self.devices.get(device_id)
                if not state:
                    continue

                # Skip devices that are offline or in error state
                if state.status not in (DeviceStatus.ONLINE, DeviceStatus.BUSY):
                    continue

                # Try to acquire lock without blocking
                acquired = state.lock.locked() is False
                if acquired:
                    try:
                        async with state.lock:
                            state.status = DeviceStatus.BUSY
                            try:
                                data = await self._read_from_device(device_id, num_bytes)
                                state.bytes_served += num_bytes
                                state.requests_served += 1
                                state.last_request_time = time.time()
                                return data, device_id
                            finally:
                                if state.status == DeviceStatus.BUSY:
                                    state.status = DeviceStatus.ONLINE
                    except Exception as e:
                        state.status = DeviceStatus.ERROR
                        state.error_message = str(e)
                        logger.error(f"Device {device_id} read error: {e}")
                        # Continue to try next device
                        continue

            # No device available, wait a bit and retry
            await asyncio.sleep(0.01)

        raise TimeoutError(f"No device available within {timeout} seconds")

    async def _read_from_device(self, device_id: str, num_bytes: int) -> bytes:
        """
        Read bytes from a device. Called while holding the device lock.

        Streaming devices (streaming_mode: true):
          If sleeping, restarts continuous mode first (wake).
          Reads via read_continuous() — no size limit.

        One-shot devices (streaming_mode: false):
          Uses start_one_shot() up to the device's one-shot limit
          (35,200 bytes for Firefly, 13,440 for QCicada).
          Uses a temporary continuous burst for larger requests.
        """
        state = self.devices[device_id]

        if not state.qcc_device:
            # Try to reconnect
            await self._connect_device(device_id)
            # For streaming devices, restart continuous mode after reconnect
            if state.config.streaming_mode:
                await self._start_streaming(device_id)

        if state.config.streaming_mode:
            if not state.streaming_active:
                # Device is sleeping — wake it up
                await self._start_streaming(device_id)
                logger.info(f"Device {device_id} waking from sleep mode")
            return await self._read_streaming(device_id, num_bytes)

        # One-shot path — limit differs by device type:
        #   Firefly (PCIe): 35,200 bytes  (20 × 1,760-byte block, per get_status ready_bytes)
        #   QCicada (USB):  13,440 bytes
        MAX_ONESHOT_BYTES = 35200 if state.config.type == "firefly" else 13440

        if num_bytes > MAX_ONESHOT_BYTES:
            return await self._read_large_continuous(device_id, num_bytes)

        loop = asyncio.get_running_loop()

        def _read_oneshot():
            data = state.qcc_device.start_one_shot(num_bytes)
            if data is None:
                raise RuntimeError("Device returned no data (None)")
            if len(data) == 0:
                raise RuntimeError("Device returned empty data")
            return bytes(data)

        data = await loop.run_in_executor(self._executor, _read_oneshot)

        if len(data) < num_bytes:
            raise RuntimeError(
                f"Device {device_id} returned {len(data)} bytes, expected {num_bytes}"
            )

        return data

    async def _read_streaming(self, device_id: str, num_bytes: int) -> bytes:
        """
        Read from a device already in continuous mode.

        No size limit. Called while holding the device lock.
        """
        state = self.devices[device_id]
        loop = asyncio.get_running_loop()

        def _do_read():
            _flush_input(state)
            data = state.qcc_device.read_continuous(num_bytes)
            if data is None or len(data) == 0:
                raise RuntimeError("Device returned no data in streaming mode")
            return bytes(data)

        data = await loop.run_in_executor(self._executor, _do_read)

        if len(data) < num_bytes:
            raise RuntimeError(
                f"Device {device_id} returned {len(data)} bytes, expected {num_bytes}"
            )

        return data

    async def _read_large_continuous(self, device_id: str, num_bytes: int) -> bytes:
        """
        Read more than the one-shot limit from a one-shot device by temporarily
        using continuous mode.
        """
        state = self.devices[device_id]
        loop = asyncio.get_running_loop()

        def _read_continuous():
            _flush_input(state)
            if not state.qcc_device.start_continuous():
                raise RuntimeError("Failed to start continuous mode")
            try:
                data = state.qcc_device.read_continuous(num_bytes)
                if data is None or len(data) == 0:
                    raise RuntimeError("Device returned no data in continuous mode")
                return bytes(data)
            finally:
                try:
                    state.qcc_device.stop()
                except Exception:
                    pass
                _flush_input(state)

        data = await loop.run_in_executor(self._executor, _read_continuous)

        if len(data) < num_bytes:
            raise RuntimeError(
                f"Device {device_id} returned {len(data)} bytes, expected {num_bytes}"
            )

        return data

    def get_device_status(self, device_id: str) -> Optional[dict]:
        """Get status information for a device."""
        state = self.devices.get(device_id)
        if not state:
            return None

        return {
            "id": device_id,
            "type": state.config.type,
            "path": state.config.path,
            "status": state.status.value,
            "post_processing_mode": state.config.post_processing_mode,
            "streaming_mode": state.config.streaming_mode,
            "streaming_idle_timeout": state.config.streaming_idle_timeout,
            "streaming_active": state.streaming_active,
            "bytes_served": state.bytes_served,
            "requests_served": state.requests_served,
            "last_request_time": state.last_request_time,
            "error_message": state.error_message,
            "is_available": state.is_available,
        }

    def get_all_devices_status(self) -> list[dict]:
        """Get status for all devices."""
        return [
            self.get_device_status(dev_id)
            for dev_id in self.devices
        ]


# Global device manager instance
_device_manager: Optional[DeviceManager] = None


def get_device_manager() -> DeviceManager:
    """Get the global device manager instance."""
    global _device_manager
    if _device_manager is None:
        _device_manager = DeviceManager()
    return _device_manager
