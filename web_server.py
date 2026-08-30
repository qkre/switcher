"""로컬망 웹 서버 - 아이폰 등 다른 기기의 브라우저로 스위처를 제어한다.

블루투스는 이 PC 가 물고 있고, 폰은 이 페이지만 열면 된다.
같은 공유기(같은 대역)에 붙어 있어야 한다.

    venv\\Scripts\\python.exe web_server.py
    venv\\Scripts\\python.exe web_server.py --port 8080

주의: gui.py 와 동시에 실행하지 말 것.
스위처는 BLE 연결을 하나만 받으므로 두 프로세스가 연결을 서로 뺏는다.

선택: 환경변수 SWITCHER_WEB_PIN 을 정하면 페이지 진입 시 PIN 을 묻는다.
"""
import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import schedules
import switcher_link

# gui.py 와 같은 규칙
CONFIG_PATH = os.environ.get("SWITCHER_CONFIG") or os.path.join(
    os.path.dirname(__file__), "config.json")
PIN = os.environ.get("SWITCHER_WEB_PIN", "").strip()

MAX_BODY = 4096

_LOGGER = logging.getLogger(__name__)


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:  # BOM 허용
            return json.load(f)
    except Exception:
        return {}


def lan_ips():
    """이 PC 가 로컬망에서 갖는 IPv4 주소들."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        # 실제로 패킷을 보내지는 않는다. 기본 경로에 쓰이는 주소를 알아내는 용도.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ips.add(probe.getsockname()[0])
        finally:
            probe.close()
    except Exception:
        pass
    return sorted(ip for ip in ips
                  if not ip.startswith("127.") and not ip.startswith("169.254."))


PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")


def load_page():
    with open(PAGE_PATH, "r", encoding="utf-8") as f:
        return f.read()


class SwitchError(Exception):
    """명령을 보낼 수 없거나 보내다 실패했을 때."""

    def __init__(self, message, code=400, done=None, failed=None):
        super().__init__(message)
        self.code = code
        self.done = done or []
        self.failed = failed


def resolve_targets(switch_raw, type_num):
    """'1' / '2' / 'all' 을 실제 스위치 번호 목록으로 바꾼다."""
    raw = str(switch_raw).strip().lower()
    if raw in ("all", "both"):
        return [1, 2] if type_num == 2 else [1]
    try:
        return [int(raw)]
    except (TypeError, ValueError):
        return []


def apply_switch(link, cfg, switch_raw, action_raw):
    """설정(1구/2구, invert)을 반영해 실제 명령을 보낸다.

    HTTP 요청과 예약 실행이 같은 경로를 쓰도록 여기에 모아 둔다.
    성공한 스위치 번호 목록과 실제로 보낸 action 을 돌려준다.
    """
    type_num = int(cfg.get("type", 2))
    action = str(action_raw).strip().lower()
    targets = resolve_targets(switch_raw, type_num)

    if action not in ("on", "off") or not targets \
            or any(t not in (1, 2) for t in targets):
        raise SwitchError("switch 는 1, 2, all 중 하나이고 action 은 on 또는 off")
    if 2 in targets and type_num == 1:
        raise SwitchError("1구 설정인데 스위치2 를 요청했습니다.")

    mac = str(cfg.get("mac", "")).strip()
    if not mac:
        raise SwitchError("config.json 에 MAC 주소가 없습니다.")
    if cfg.get("invert", False):
        action = "off" if action == "on" else "on"

    keep_alive = bool(cfg.get("keep_alive", True))
    done = []
    for index in targets:
        try:
            link.send(mac, index, action, keep_alive=keep_alive)
        except Exception as e:
            # 여러 개 중 일부만 성공했으면 어디까지 됐는지 알려준다
            raise SwitchError(f"{type(e).__name__}: {e}",
                              code=502, done=done, failed=index)
        done.append(index)
    return done, action


class Handler(BaseHTTPRequestHandler):
    server_version = "SwitcherWeb/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _pin_ok(self):
        if not PIN:
            return True
        if self.headers.get("X-Pin", "") == PIN:
            return True
        # 단축어에서는 헤더보다 ?pin=... 이 넣기 쉽다
        query = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(query).get("pin", [""])[0] == PIN

    def log_message(self, fmt, *args):
        _LOGGER.info("%s - %s", self.address_string(), fmt % args)

    # ---------- routes ----------

    def _read_json_body(self):
        """본문을 dict 로 읽는다. 못 읽으면 응답까지 보내고 None 을 돌려준다."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY:
            self._json(400, {"error": "잘못된 요청 본문"})
            return None
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"error": "JSON 을 읽을 수 없습니다."})
            return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, self.server.page, "text/html; charset=utf-8")
            return
        if path == "/api/schedules":
            if not self._pin_ok():
                self._json(401, {"error": "PIN 이 필요합니다."})
                return
            self._json(200, {"schedules": self.server.schedules.list()})
            return
        if path == "/api/status":
            if not self._pin_ok():
                self._json(401, {"error": "PIN 이 필요합니다."})
                return
            cfg = self.server.config
            self._json(200, {
                "mac": cfg.get("mac", ""),
                "type": cfg.get("type", 2),
                "invert": bool(cfg.get("invert", False)),
                "connected": self.server.link.connected,
            })
            return

        # 아이폰 단축어/시리용 간편 경로:  /s/1/on   /s/2/off
        # 단축어의 "URL 콘텐츠 가져오기" 는 GET 이 기본이라 POST + JSON 보다 훨씬 쉽다.
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "s":
            self._do_switch(parts[1], parts[2])
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._pin_ok():
            self._json(401, {"error": "PIN 이 필요합니다."})
            return
        parts = [p for p in path.split("/") if p]

        if path == "/api/switch":
            body = self._read_json_body()
            if body is None:
                return
            try:
                self._do_switch(body["switch"], body["action"])
            except (KeyError, TypeError):
                self._json(400, {"error": "switch/action 값을 읽을 수 없습니다."})
            return

        if path == "/api/connect":
            body = self._read_json_body()
            if body is None:
                return
            link = self.server.link
            mac = str(self.server.config.get("mac", "")).strip()
            if not mac:
                self._json(400, {"error": "config.json 에 MAC 주소가 없습니다."})
                return
            if body.get("rescan"):
                # 캐시된 기기 정보를 버리고 처음부터 다시 찾는다
                link.forget()
            try:
                link.connect(mac)
            except Exception as e:
                self._json(502, {"error": f"{type(e).__name__}: {e}",
                                 "connected": link.connected})
                return
            self._json(200, {"ok": True, "connected": link.connected})
            return

        if path == "/api/disconnect":
            if self._read_json_body() is None:
                return
            self.server.link.disconnect()
            self._json(200, {"ok": True, "connected": self.server.link.connected})
            return

        # /api/schedules            예약 추가
        # /api/schedules/<id>       예약 수정 (부분 갱신)
        # /api/schedules/<id>/delete  예약 삭제
        if parts[:2] == ["api", "schedules"]:
            body = self._read_json_body()
            if body is None:
                return
            store = self.server.schedules
            try:
                if len(parts) == 2:
                    self._json(200, {"schedule": store.add(body)})
                    return
                if len(parts) == 3:
                    item = store.update(parts[2], body)
                    if item is None:
                        self._json(404, {"error": "그런 예약이 없습니다."})
                    else:
                        self._json(200, {"schedule": item})
                    return
                if len(parts) == 4 and parts[3] == "delete":
                    if store.remove(parts[2]):
                        self._json(200, {"ok": True, "removed": parts[2]})
                    else:
                        self._json(404, {"error": "그런 예약이 없습니다."})
                    return
            except schedules.ScheduleError as e:
                self._json(400, {"error": str(e)})
                return

        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if not self._pin_ok():
            self._json(401, {"error": "PIN 이 필요합니다."})
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "schedules"]:
            if self.server.schedules.remove(parts[2]):
                self._json(200, {"ok": True, "removed": parts[2]})
            else:
                self._json(404, {"error": "그런 예약이 없습니다."})
            return
        self._json(404, {"error": "not found"})

    # ---------- 공통 처리 ----------

    def _do_switch(self, switch_raw, action_raw):
        if not self._pin_ok():
            self._json(401, {"error": "PIN 이 필요합니다."})
            return
        try:
            done, action = apply_switch(self.server.link, self.server.config,
                                        switch_raw, action_raw)
        except SwitchError as e:
            payload = {"error": str(e)}
            if e.done:
                payload["done"] = e.done
            if e.failed is not None:
                payload["failed"] = e.failed
            self._json(e.code, payload)
            return
        self._json(200, {
            "ok": True,
            "switches": done,
            "action": action,
            "connected": self.server.link.connected,
        })


# ---------- 절전 방지 (Windows) ----------
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def prevent_sleep(enable=True):
    """서버가 도는 동안 PC 가 유휴 절전에 들어가지 않게 한다.

    이 PC 는 S3 절전이라 한번 잠들면 프로세스가 통째로 멈추고 BLE 연결도
    끊긴다. 깨어 있는 채로 연결을 유지할 방법이 없으므로 아예 잠들지 않게 막는다.

    막을 수 있는 것은 '유휴 시간 초과로 인한 절전' 뿐이다.
    사용자가 직접 절전을 누르거나 노트북 덮개를 닫으면 그대로 잠든다.
    SetThreadExecutionState 는 호출한 스레드에 묶이므로 메인 스레드에서 부른다.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if enable else ES_CONTINUOUS
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as e:
        _LOGGER.debug("절전 방지 설정 실패: %s", e)
        return False


def keep_warm(link, cfg, interval=120.0):
    """미리 연결해 두고, 끊겨 있으면 다시 붙인다.

    첫 명령의 스캔 지연(10초 이상) 때문에 시리가 기다리다 실패하는 걸 막는다.
    재연결은 캐시된 BLEDevice 를 쓰므로 스캔 없이 빠르다.
    """
    mac = str(cfg.get("mac", "")).strip()
    if not mac:
        return
    while True:
        if cfg.get("keep_alive", True) and not link.connected:
            try:
                link.connect(mac)
                _LOGGER.info("스위처 연결 준비됨 (%s)", mac)
            except Exception as e:
                _LOGGER.debug("연결 준비 실패, 나중에 다시 시도: %s", e)
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description="스위처 로컬망 웹 서버")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0",
                        help="기본값은 모든 인터페이스. 127.0.0.1 이면 이 PC 에서만 접속 가능.")
    parser.add_argument("--no-warmup", action="store_true",
                        help="미리 연결해 두지 않는다 (첫 명령이 느려진다).")
    parser.add_argument("--allow-sleep", action="store_true",
                        help="PC 가 유휴 절전에 들어가도 막지 않는다 (연결이 끊긴다).")
    parser.add_argument("--log-file", default=None,
                        help="로그를 파일에도 남긴다 (창 없이 띄울 때 필요).")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="서버 안의 예약 실행을 끈다 "
                             "(schedule_tasks.py 로 작업 스케줄러에 내보낸 경우).")
    args = parser.parse_args(argv)

    # pythonw.exe 로 창 없이 띄우면 화면 출력이 사라지므로 파일 로그를 남길 수 있게 한다
    # pythonw.exe 로 띄우면 sys.stderr/stdout 이 None 이라 StreamHandler 가 터진다
    handlers = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if args.log_file:
        try:
            handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
        except Exception as e:
            print(f"로그 파일을 열지 못했습니다({args.log_file}): {e}")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s",
                        datefmt="%m-%d %H:%M:%S",
                        handlers=handlers)
    # 창 없이(pythonw) 띄운 경우 print 는 사라지므로 안내문도 로거로 내보낸다
    say = _LOGGER.info if args.log_file else print

    cfg = load_config()
    if not str(cfg.get("mac", "")).strip():
        say("경고: config.json 에 MAC 주소가 없습니다. gui.py 에서 먼저 설정하세요.")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.config = cfg
    httpd.page = load_page()
    httpd.link = switcher_link.SwitcherLink()
    httpd.schedules = schedules.ScheduleStore()

    awake = False if args.allow_sleep else prevent_sleep(True)

    scheduler = None
    if not args.no_scheduler:
        scheduler = schedules.Scheduler(
            httpd.schedules,
            lambda switch, action: apply_switch(httpd.link, cfg, switch, action))
        scheduler.start()

    say("=" * 52)
    say(f"  스위처 웹 서버 시작  (MAC {cfg.get('mac', '?')}, {cfg.get('type', 2)}구)")
    say(f"  PIN: {'사용 중' if PIN else '없음 (같은 공유기의 누구나 접속 가능)'}")
    if args.allow_sleep:
        say("  절전 방지: 꺼짐 - PC 가 잠들면 연결도 끊깁니다.")
    elif awake:
        say("  절전 방지: 켜짐 - 서버가 도는 동안 PC 가 잠들지 않습니다.")
        say("             (덮개를 닫거나 직접 절전을 누르면 그래도 잠듭니다)")
    else:
        say("  절전 방지: 실패 - PC 가 잠들면 연결이 끊깁니다.")
    say("-" * 52)
    say("  아이폰 사파리에서 아래 주소를 여세요:")
    for ip in lan_ips():
        say(f"      http://{ip}:{args.port}")
    say(f"  이 PC 에서 확인:  http://127.0.0.1:{args.port}")
    say("-" * 52)
    say("  시리/단축어용 주소:")
    type_num = int(cfg.get("type", 2))
    for ip in lan_ips()[:1]:
        base = f"http://{ip}:{args.port}"
        rows = [("/s/1/on", "스위치1 켜기"), ("/s/1/off", "스위치1 끄기")]
        if type_num == 2:
            rows += [("/s/2/on", "스위치2 켜기"), ("/s/2/off", "스위치2 끄기"),
                     ("/s/all/on", "둘 다 켜기"), ("/s/all/off", "둘 다 끄기")]
        width = max(len(path) for path, _ in rows)
        for path, label in rows:
            say(f"      {base}{path:<{width}}   ({label})")
    say("-" * 52)
    saved = httpd.schedules.list()
    if args.no_scheduler:
        say("  예약: 서버 안 실행 꺼짐 (작업 스케줄러가 담당)")
    if saved:
        say(f"  예약 {len(saved)}건:")
        for item in saved:
            mark = " " if item["enabled"] else "×"
            target = "둘 다" if item["switch"] == "all" else f"스위치{item['switch']}"
            what = "켜기" if item["action"] == "on" else "끄기"
            say(f"    {mark} {item['time']}  {target} {what}  "
                  f"({schedules.days_text(item['days'])})")
    else:
        say("  예약 없음 - 웹 페이지에서 추가하세요.")
    say("-" * 52)
    say("  종료: Ctrl+C")
    say("=" * 52)
    if sys.stdout is not None:
        sys.stdout.flush()  # 파이프로 넘길 때도 안내문이 바로 보이도록

    if not args.no_warmup:
        threading.Thread(target=keep_warm, args=(httpd.link, cfg),
                         daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다...")
    finally:
        prevent_sleep(False)  # 절전 방지 해제
        if scheduler:
            scheduler.stop()
        httpd.server_close()
        httpd.link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
