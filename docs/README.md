# 스위처 — 아이폰 직접 연결 페이지

`index.html` 한 장짜리 정적 페이지입니다. 폰이 스위처에 **직접** BLE 로 붙습니다.
PC 도, 웹 서버도 필요 없습니다.

## GitHub Pages 로 올리기

1. 이 프로젝트를 GitHub 저장소에 올립니다 (`docs/` 폴더 포함).
2. 저장소 **Settings > Pages** 로 갑니다.
3. **Source** 를 `Deploy from a branch`, **Branch** 를 `main` / `/docs` 로 지정하고 저장.
4. 1~2분 뒤 `https://<사용자명>.github.io/<저장소이름>/` 이 열립니다.

## 아이폰에서 쓰기

iOS 사파리와 크롬은 Web Bluetooth 를 지원하지 않습니다. 반드시 아래 브라우저가 필요합니다.

1. App Store 에서 **Bluefy – Web BLE Browser** 를 설치합니다.
2. Bluefy 에서 위 https 주소를 엽니다.
3. **연결하기** → 목록에서 `SWITCHER_M` 선택.
4. ON/OFF 를 누릅니다.

`http://` 나 `file://` 로는 동작하지 않습니다 (Web Bluetooth 는 보안 컨텍스트 필수).

## 값의 출처

`android/flutter_app/lib/main.dart` 및 `pyswitcherio` 와 동일합니다.

| | 값 |
|---|---|
| 서비스 UUID | `0000150b-0000-1000-8000-00805f9b34fb` |
| 캐릭터리스틱 UUID | `000015ba-0000-1000-8000-00805f9b34fb` |
| 1구 ON / OFF | `0x00` / `0x01` |
| 2구 ON / OFF | `0x05` / `0x03` |

## 안 되는 것

- **시리 명령** — 단축어는 BLE GATT 쓰기를 할 수 없습니다.
- **예약** — 페이지가 닫히면 실행되지 않습니다.

둘 다 필요하면 PC 의 `web_server.py` 를 함께 쓰세요. 단, 스위처는 BLE 연결을
하나만 받으므로 폰과 PC 가 동시에 연결하면 서로 뺏습니다.

## 시리 / 단축어 (시험적)

단축어 앱에는 BLE 명령 액션이 없습니다. 대신 주소에 명령을 실어 Bluefy 로 열면
페이지가 뜨자마자 알아서 전송합니다.

    https://<주소>/?do=1:on      스위치1 켜기
    https://<주소>/?do=1:off     스위치1 끄기
    https://<주소>/?do=2:on      스위치2 켜기
    https://<주소>/?do=2:off     스위치2 끄기
    https://<주소>/?do=all:on    둘 다 켜기
    https://<주소>/?do=all:off   둘 다 끄기

단축어 구성: [URL] 액션에 위 주소 -> [URL 열기] 액션. 이름을 "불 꺼" 처럼 지으면
시리가 그 이름을 알아듣습니다.

되는지 여부는 Bluefy 가 `navigator.bluetooth.getDevices()` 를 지원하는지에 달려 있습니다.
페이지 맨 아래 "설정" 카드에 지원 여부가 표시됩니다.

  * 지원함  -> 기기 선택창 없이 자동 전송. 시리로 쓸 수 있습니다.
  * 미지원 -> 열 때마다 기기를 손으로 골라야 해서 시리로는 못 씁니다.

어느 쪽이든 Bluefy 앱이 화면에 뜨는 것 자체는 막을 수 없습니다.
완전히 손 안 대는 시리 제어는 PC 의 web_server.py 방식만 가능합니다.
