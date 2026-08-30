"""가짜 링크로 API 라우팅만 검증한다 (실제 전등은 건드리지 않음)."""
import json, threading, urllib.request, urllib.error, sys
sys.path.insert(0, r"C:\Users\JH\Downloads\io-switcher-local-1\io-switcher-local-1")
from http.server import ThreadingHTTPServer
import web_server

sent = []

class FakeLink:
    connected = True
    def send(self, mac, index, action, keep_alive=True):
        sent.append((index, action))
        return True
    def close(self): pass

httpd = ThreadingHTTPServer(("127.0.0.1", 8099), web_server.Handler)
httpd.daemon_threads = True
httpd.link = FakeLink()

def call(path, cfg):
    httpd.config = cfg
    del sent[:]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8099{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode()), list(sent)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), list(sent)

threading.Thread(target=httpd.serve_forever, daemon=True).start()

TWO = {"mac": "AA:BB:CC:DD:EE:FF", "type": 2, "invert": False, "keep_alive": True}
ONE = {"mac": "AA:BB:CC:DD:EE:FF", "type": 1, "invert": False, "keep_alive": True}
INV = {"mac": "AA:BB:CC:DD:EE:FF", "type": 2, "invert": True,  "keep_alive": True}

cases = [
    ("/s/1/on",    TWO, 200, [(1, "on")]),
    ("/s/1/off",   TWO, 200, [(1, "off")]),
    ("/s/2/on",    TWO, 200, [(2, "on")]),
    ("/s/2/off",   TWO, 200, [(2, "off")]),
    ("/s/all/on",  TWO, 200, [(1, "on"), (2, "on")]),
    ("/s/all/off", TWO, 200, [(1, "off"), (2, "off")]),
    ("/s/both/on", TWO, 200, [(1, "on"), (2, "on")]),
    ("/s/all/on",  ONE, 200, [(1, "on")]),          # 1구면 스위치1만
    ("/s/2/on",    ONE, 400, []),                   # 1구인데 스위치2 -> 거부
    ("/s/1/on",    INV, 200, [(1, "off")]),         # invert 적용
    ("/s/all/on",  INV, 200, [(1, "off"), (2, "off")]),
    ("/s/3/on",    TWO, 400, []),
    ("/s/1/toggle",TWO, 400, []),
]

fails = 0
for path, cfg, want_code, want_sent in cases:
    code, body, got = call(path, cfg)
    ok = (code == want_code and got == want_sent)
    fails += (not ok)
    tag = "OK  " if ok else "FAIL"
    label = f"{path}  (type={cfg['type']}, invert={cfg['invert']})"
    print(f"{tag} {label:<44} HTTP {code}  sent={got}")
    if not ok:
        print(f"     기대: HTTP {want_code} sent={want_sent}  응답={body}")

print()
print("모두 통과" if not fails else f"{fails}건 실패")

# ---------- 예약 API ----------
import os, tempfile
import schedules as sched_mod
httpd.schedules = sched_mod.ScheduleStore(
    os.path.join(tempfile.mkdtemp(prefix="webapi_"), "schedules.json"))
httpd.config = TWO

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:8099{path}", data=data,
                               method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

wfails = 0
def wcheck(label, got, want):
    global wfails
    ok = got == want
    wfails += (not ok)
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     기대: {want}\n     실제: {got}")

code, body = req("GET", "/api/schedules")
wcheck("GET  /api/schedules (빈 목록)", (code, body), (200, {"schedules": []}))

code, body = req("POST", "/api/schedules",
                 {"time": "07:30", "days": [0,1,2,3,4], "switch": "all",
                  "action": "on", "label": "아침"})
wcheck("POST /api/schedules 추가", code, 200)
sid = body["schedule"]["id"]
wcheck("  저장된 값", (body["schedule"]["time"], body["schedule"]["switch"]),
       ("07:30", "all"))

code, body = req("POST", "/api/schedules", {"time": "99:99", "switch": "1", "action": "on"})
wcheck("POST 잘못된 시각 -> 400", code, 400)

code, body = req("POST", f"/api/schedules/{sid}", {"enabled": False})
wcheck("POST /api/schedules/<id> 끄기", (code, body["schedule"]["enabled"]), (200, False))

code, body = req("POST", "/api/schedules/nope", {"enabled": False})
wcheck("없는 id 수정 -> 404", code, 404)

code, body = req("GET", "/api/schedules")
wcheck("목록에 1건", len(body["schedules"]), 1)

code, body = req("POST", f"/api/schedules/{sid}/delete", {})
wcheck("POST .../delete", (code, body.get("ok")), (200, True))

code, body = req("GET", "/api/schedules")
wcheck("삭제 후 빈 목록", body["schedules"], [])

code, body = req("POST", "/api/schedules",
                 {"time": "22:00", "days": None, "switch": "1", "action": "off"})
sid2 = body["schedule"]["id"]
code, body = req("DELETE", f"/api/schedules/{sid2}")
wcheck("DELETE /api/schedules/<id>", (code, body.get("ok")), (200, True))

code, body = req("GET", "/nope")
wcheck("GET  /nope -> 404", code, 404)

print()
print("예약 API 모두 통과" if not wfails else f"예약 API {wfails}건 실패")
httpd.shutdown()
sys.exit(1 if (fails or wfails) else 0)
