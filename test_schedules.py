"""예약 저장/실행 테스트. 가짜 시계와 가짜 링크를 써서 실제 전등은 건드리지 않는다."""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedules

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += (not ok)
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     기대: {want}\n     실제: {got}")


tmp = tempfile.mkdtemp(prefix="sched_test_")
path = os.path.join(tmp, "schedules.json")
store = schedules.ScheduleStore(path)

# ---------- 저장 ----------
a = store.add({"time": "7:5", "days": [0, 1, 2, 3, 4], "switch": "1",
               "action": "on", "label": "아침"})
check("시각 정규화 7:5 -> 07:05", a["time"], "07:05")
check("요일 저장", a["days"], [0, 1, 2, 3, 4])

b = store.add({"time": "23:30", "days": None, "switch": "all", "action": "off"})
check("days=None 이면 매일", b["days"], schedules.ALL_DAYS)
check("both -> all 정규화",
      store.add({"time": "12:00", "days": [6], "switch": "both",
                 "action": "on"})["switch"], "all")

check("시각순 정렬", [s["time"] for s in store.list()],
      ["07:05", "12:00", "23:30"])

# ---------- 검증 실패 ----------
for bad, why in [
    ({"time": "25:00", "days": [0], "switch": "1", "action": "on"}, "시각 범위"),
    ({"time": "07:00", "days": [9], "switch": "1", "action": "on"}, "요일 범위"),
    ({"time": "07:00", "days": [], "switch": "1", "action": "on"}, "빈 요일"),
    ({"time": "07:00", "days": [0], "switch": "3", "action": "on"}, "잘못된 switch"),
    ({"time": "07:00", "days": [0], "switch": "1", "action": "toggle"}, "잘못된 action"),
    ({"time": "0700", "days": [0], "switch": "1", "action": "on"}, "형식 없는 시각"),
]:
    try:
        store.add(bad)
        check(f"거부되어야 함 ({why})", "통과됨", "ScheduleError")
    except schedules.ScheduleError:
        check(f"거부되어야 함 ({why})", "ScheduleError", "ScheduleError")

# ---------- 수정 / 삭제 ----------
check("enabled 끄기", store.update(a["id"], {"enabled": False})["enabled"], False)
check("없는 id 수정", store.update("nope", {"enabled": True}), None)
check("삭제", store.remove(b["id"]), True)
check("없는 id 삭제", store.remove("nope"), False)

# ---------- 디스크 반영 ----------
reloaded = schedules.ScheduleStore(path)
check("파일에서 다시 읽기", [s["time"] for s in reloaded.list()], ["07:05", "12:00"])
check("enabled=False 유지",
      [s["enabled"] for s in reloaded.list() if s["time"] == "07:05"], [False])

# ---------- 실행 ----------
store2 = schedules.ScheduleStore(os.path.join(tmp, "s2.json"))
store2.add({"time": "07:30", "days": [0, 1, 2, 3, 4], "switch": "1", "action": "on"})
store2.add({"time": "07:30", "days": [5, 6], "switch": "2", "action": "off"})
store2.add({"time": "23:00", "days": None, "switch": "all", "action": "off"})
store2.add({"time": "08:00", "days": None, "switch": "1", "action": "on",
            "enabled": False})

sent = []
clock = {"now": datetime(2026, 8, 31, 7, 30)}  # 2026-08-31 은 월요일


def fake_send(switch, action):
    sent.append((switch, action))


sch = schedules.Scheduler(store2, fake_send, now_func=lambda: clock["now"])

sch.tick()
check("월요일 07:30 -> 평일 예약만", list(sent), [("1", "on")])

del sent[:]
sch.tick()
check("같은 분에 두 번 실행 안 함", list(sent), [])

del sent[:]
clock["now"] = datetime(2026, 8, 31, 7, 31)
sch.tick()
check("07:31 에는 아무것도", list(sent), [])

del sent[:]
clock["now"] = datetime(2026, 9, 5, 7, 30)  # 토요일
sch.tick()
check("토요일 07:30 -> 주말 예약", list(sent), [("2", "off")])

del sent[:]
clock["now"] = datetime(2026, 9, 5, 23, 0)
sch.tick()
check("매일 23:00 -> all off", list(sent), [("all", "off")])

del sent[:]
clock["now"] = datetime(2026, 9, 5, 8, 0)
sch.tick()
check("enabled=False 는 실행 안 함", list(sent), [])

del sent[:]
clock["now"] = datetime(2026, 9, 7, 7, 30)  # 다음 월요일
sch.tick()
check("날짜가 바뀌면 다시 실행", list(sent), [("1", "on")])


def boom(switch, action):
    raise RuntimeError("BLE 실패")


sch2 = schedules.Scheduler(store2, boom, now_func=lambda: datetime(2026, 9, 7, 23, 0))
try:
    sch2.tick()
    check("실행 실패해도 예외 안 터짐", "정상 반환", "정상 반환")
except Exception as e:
    check("실행 실패해도 예외 안 터짐", f"{type(e).__name__}", "정상 반환")

print()
print("모두 통과" if not fails else f"{fails}건 실패")
sys.exit(1 if fails else 0)
