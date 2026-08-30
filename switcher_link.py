"""스위처와의 BLE 연결을 유지·재사용하는 계층.

pyswitcherio 의 IOSwitcher 는 명령을 보낼 때마다
  1. BleakScanner.find_device_by_address() 로 전체 스캔 (기본 10초)
  2. 연결 -> 쓰기
  3. finally 에서 무조건 연결 해제
를 반복한다. 그래서 버튼을 누를 때마다 처음부터 다시 시작한다.

여기서는 명령 바이트와 UUID 만 가져다 쓰고 연결 관리는 직접 한다:
  * BLEDevice 를 캐시해서 재스캔을 생략한다
  * keep_alive 면 BleakClient 를 연결된 채로 들고 있는다
  * 연결이 끊겨 있으면 캐시된 BLEDevice 로 즉시 재연결한다

bleak 객체는 자신을 만든 이벤트 루프에서만 사용해야 하므로,
전용 스레드에 이벤트 루프를 하나 띄워 두고 모든 호출을 그 루프로 넘긴다.
"""
import asyncio
import logging
import threading

from bleak import BleakClient, BleakScanner
import pyswitcherio

# android/flutter_app/lib/main.dart 및 pyswitcherio 와 동일
SERVICE_UUID = "0000150b-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "000015ba-0000-1000-8000-00805f9b34fb"

# 명령 바이트는 pyswitcherio 를 단일 출처로 삼는다 (1구: 00/01, 2구: 05/03)
KEYS = {
    (1, "on"): pyswitcherio.ON_KEY1,
    (1, "off"): pyswitcherio.OFF_KEY1,
    (2, "on"): pyswitcherio.ON_KEY2,
    (2, "off"): pyswitcherio.OFF_KEY2,
}

FIND_TIMEOUT = 12.0      # MAC 으로 기기를 찾는 스캔 시간
CONNECT_TIMEOUT = 20.0   # BLE 연결 시도 시간
CALL_TIMEOUT = 60.0      # 호출 스레드가 결과를 기다리는 최대 시간

_LOGGER = logging.getLogger(__name__)


class SwitcherLink:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._call_lock = threading.Lock()  # 명령 직렬화
        self._device = None
        self._device_mac = None
        self._client = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout=CALL_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise TimeoutError(f"{int(timeout)}초 안에 응답이 없습니다.")

    # ---------- 바깥에서 부르는 것 (아무 스레드나 가능) ----------

    def send(self, mac, switch_index, action, keep_alive=True):
        """명령을 보낸다. 실패하면 예외를 던진다."""
        try:
            key = KEYS[(switch_index, action)]
        except KeyError:
            raise ValueError(f"알 수 없는 명령: switch{switch_index} {action}")
        with self._call_lock:
            return self._submit(self._send(mac, key, keep_alive))

    @property
    def connected(self):
        client = self._client
        return bool(client is not None and client.is_connected)

    def connect(self, mac):
        """명령 없이 연결만 미리 해 둔다.

        첫 명령은 MAC 으로 기기를 찾는 스캔 때문에 10초 넘게 걸린다.
        서버 시작 직후 이걸 불러 두면 실제 명령이 바로 나간다.
        """
        with self._call_lock:
            self._submit(self._connect_only(mac))
        return True

    def forget(self):
        """연결을 끊고 캐시된 기기 정보까지 버린다.

        스위처를 옮겼거나 한참 안 잡힐 때, 다음 연결에서 처음부터 다시
        스캔하도록 만든다.
        """
        self.disconnect()
        self._device = None
        self._device_mac = None

    def disconnect(self):
        """연결만 끊는다. 기기 캐시는 남겨서 다음 연결을 빠르게 한다."""
        with self._call_lock:
            try:
                self._submit(self._drop_client(), timeout=20.0)
            except Exception as e:
                _LOGGER.debug("연결 해제 실패(무시): %s", e)

    def close(self):
        self.disconnect()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # ---------- 이벤트 루프 안에서만 실행되는 것 ----------

    async def _send(self, mac, key, keep_alive):
        try:
            await self._write(mac, key)
        except Exception as first:
            # 캐시된 연결/기기 정보가 낡아서 실패했을 수 있다. 한 번만 비우고 재시도.
            _LOGGER.debug("첫 시도 실패, 캐시를 버리고 재시도: %s", first)
            await self._drop_client()
            self._device = None
            self._device_mac = None
            await self._write(mac, key)
        if not keep_alive:
            await self._drop_client()
        return True

    async def _connect_only(self, mac):
        try:
            await self._ensure_client(mac)
        except Exception:
            # 캐시가 낡았을 수 있으니 한 번만 비우고 재시도
            await self._drop_client()
            self._device = None
            self._device_mac = None
            await self._ensure_client(mac)
        return True

    async def _write(self, mac, key):
        client = await self._ensure_client(mac)
        # response 인자는 기본값을 써서 pyswitcherio 와 동작을 맞춘다
        await client.write_gatt_char(CHAR_UUID, key)

    async def _ensure_device(self, mac):
        if self._device is not None and self._device_mac == mac:
            return self._device
        device = await BleakScanner.find_device_by_address(mac, timeout=FIND_TIMEOUT)
        if device is None:
            raise RuntimeError(
                f"스위처({mac})를 찾지 못했습니다. 신호가 약하거나 꺼져 있습니다.")
        self._device = device
        self._device_mac = mac
        return device

    async def _ensure_client(self, mac):
        mac = mac.strip().upper()
        if self._client is not None:
            # bleak 의 is_connected 는 property 다 (호출하면 TypeError)
            if self._client.is_connected and self._device_mac == mac:
                return self._client
            await self._drop_client()
        device = await self._ensure_device(mac)
        client = BleakClient(device, timeout=CONNECT_TIMEOUT)
        await client.connect()
        self._client = client
        _LOGGER.debug("스위처 연결 완료: %s", mac)
        return client

    async def _drop_client(self):
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:
            _LOGGER.debug("연결 해제 중 오류(무시): %s", e)
