"""
실제 BLE 스캔 진단 스크립트 (목킹 없음).

주변의 모든 BLE 광고를 그대로 덤프하고, 스위처로 의심되는 장치를 표시한다.
    python scan_debug.py            # 기본 20초 스캔
    python scan_debug.py 40         # 40초 스캔
"""
import asyncio
import sys

from bleak import BleakScanner

# android/flutter_app/lib/main.dart 의 SERVICE_UUID 와 동일
SERVICE_UUID = "0000150b-0000-1000-8000-00805f9b34fb"
NAME_HINT = "switcher"

seen = {}


def on_detect(device, adv):
    prev = seen.get(device.address)
    # 이름은 광고마다 있다가 없다가 하므로, 한 번이라도 잡힌 이름을 유지한다
    name = device.name or adv.local_name or (prev["name"] if prev else None)
    seen[device.address] = {
        "name": name,
        "rssi": adv.rssi,
        # set 으로 계속 들고 간다 (출력할 때만 정렬)
        "uuids": {u.lower() for u in adv.service_uuids} | (prev["uuids"] if prev else set()),
        "mfr": sorted(adv.manufacturer_data.keys()),
        "count": (prev["count"] if prev else 0) + 1,
    }


def is_switcher(addr, info):
    if info["name"] and NAME_HINT in info["name"].lower():
        return True
    return SERVICE_UUID in info["uuids"]


async def main(duration):
    print(f"{duration}초 동안 스캔합니다. 스위처 옆에서 실행하세요...\n")
    scanner = BleakScanner(detection_callback=on_detect)
    try:
        await scanner.start()
    except Exception as e:
        print(f"[스캔 시작 실패] {type(e).__name__}: {e}")
        print("-> 블루투스가 꺼져 있거나, 어댑터가 없거나, 권한이 없습니다.")
        return
    try:
        await asyncio.sleep(duration)
    finally:
        await scanner.stop()

    if not seen:
        print("BLE 장치를 하나도 못 찾았습니다.")
        print("-> 블루투스 어댑터/권한 문제입니다. 스위처 문제가 아닙니다.")
        return

    hits = {a: i for a, i in seen.items() if is_switcher(a, i)}

    print(f"총 {len(seen)}개 장치 발견 (이름 있는 것: "
          f"{sum(1 for i in seen.values() if i['name'])}개)\n")
    print("=== 전체 목록 (신호 강한 순) ===")
    for addr, info in sorted(seen.items(), key=lambda kv: kv[1]["rssi"], reverse=True):
        mark = " <== 스위처 의심" if addr in hits else ""
        print(f"  {addr}  RSSI {info['rssi']:>4}  x{info['count']:<3} "
              f"name={info['name']!r}{mark}")
        if info["uuids"]:
            print(f"        services: {', '.join(sorted(info['uuids']))}")
        if info["mfr"]:
            print(f"        mfr_data ids: {info['mfr']}")

    print()
    if hits:
        print("=== 스위처로 보이는 장치 ===")
        for addr, info in hits.items():
            print(f"  {addr}  name={info['name']!r}")
        print("\n위 주소를 GUI의 'MAC 주소' 칸에 넣으세요.")
    else:
        print("=== 스위처를 못 찾았습니다 ===")
        print("확인할 것:")
        print("  1. 스위처가 폰이나 다른 앱에 연결 중이면 광고를 멈춥니다. 다 끊고 다시 시도.")
        print("  2. 위 목록에서 name=None 인데 RSSI가 아주 센(-50 이상) 장치가 있는지 보세요.")
        print("     그게 스위처일 수 있습니다.")
        print("  3. Windows 설정 > 블루투스에 스위처가 페어링되어 있으면 '장치 제거' 후 재시도.")


if __name__ == "__main__":
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    asyncio.run(main(dur))
