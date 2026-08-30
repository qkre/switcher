"""예약(스케줄) 저장과 실행.

schedules.json 에 예약 목록을 저장하고, 백그라운드 스레드가 주기적으로
현재 시각을 확인해서 조건이 맞으면 명령을 보낸다.

예약 한 건의 형태:
    {
      "id": "3f9a1c22",
      "label": "아침 거실 불",
      "time": "07:30",          # 24시간, 로컬 시각
      "days": [0,1,2,3,4],      # 0=월 ... 6=일 (파이썬 weekday())
      "switch": "1",            # "1" | "2" | "all"
      "action": "on",           # "on" | "off"
      "enabled": true
    }
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime

SCHEDULES_PATH = os.environ.get("SWITCHER_SCHEDULES") or os.path.join(
    os.path.dirname(__file__), "schedules.json")

VALID_SWITCHES = ("1", "2", "all")
VALID_ACTIONS = ("on", "off")
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]

_LOGGER = logging.getLogger(__name__)


class ScheduleError(ValueError):
    """예약 내용이 올바르지 않을 때."""


def _parse_time(value):
    """'7:5' 같은 것도 '07:05' 로 정규화한다."""
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError("시각은 HH:MM 형식이어야 합니다.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ScheduleError("시각은 HH:MM 형식이어야 합니다.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError("시각 범위가 올바르지 않습니다.")
    return f"{hour:02d}:{minute:02d}"


def _parse_days(value):
    if value in (None, "", "daily", "all"):
        return list(ALL_DAYS)
    if not isinstance(value, (list, tuple)):
        raise ScheduleError("요일은 0~6 숫자 목록이어야 합니다.")
    days = set()
    for day in value:
        try:
            day = int(day)
        except (TypeError, ValueError):
            raise ScheduleError("요일은 0~6 숫자여야 합니다.")
        if not 0 <= day <= 6:
            raise ScheduleError("요일은 0(월)~6(일) 사이여야 합니다.")
        days.add(day)
    if not days:
        raise ScheduleError("요일을 하나 이상 선택하세요.")
    return sorted(days)


def normalize(raw, existing_id=None):
    """들어온 값을 검증해서 저장 가능한 형태로 만든다."""
    if not isinstance(raw, dict):
        raise ScheduleError("예약 형식이 올바르지 않습니다.")

    switch = str(raw.get("switch", "1")).strip().lower()
    if switch == "both":
        switch = "all"
    if switch not in VALID_SWITCHES:
        raise ScheduleError("switch 는 1, 2, all 중 하나여야 합니다.")

    action = str(raw.get("action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        raise ScheduleError("action 은 on 또는 off 여야 합니다.")

    label = str(raw.get("label", "")).strip()[:40]

    return {
        "id": existing_id or raw.get("id") or uuid.uuid4().hex[:8],
        "label": label,
        "time": _parse_time(raw.get("time")),
        "days": _parse_days(raw.get("days")),
        "switch": switch,
        "action": action,
        "enabled": bool(raw.get("enabled", True)),
    }


DAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]


def days_text(days):
    """[0,1,2,3,4] -> '평일' 처럼 사람이 읽기 좋게."""
    days = sorted(days)
    if days == ALL_DAYS:
        return "매일"
    if days == [0, 1, 2, 3, 4]:
        return "평일"
    if days == [5, 6]:
        return "주말"
    return " ".join(DAY_NAMES[d] for d in days)


class ScheduleStore:
    """schedules.json 을 읽고 쓴다. 여러 스레드에서 안전하게 쓸 수 있다."""

    def __init__(self, path=SCHEDULES_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._items = self._load()

    def _load(self):
        try:
            # utf-8-sig: 메모장 등이 붙인 BOM 이 있어도 읽힌다
            with open(self._path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except Exception as e:
            _LOGGER.warning("예약 파일을 읽지 못했습니다(무시): %s", e)
            return []
        items = []
        for raw in data if isinstance(data, list) else []:
            try:
                items.append(normalize(raw, existing_id=raw.get("id")))
            except ScheduleError as e:
                _LOGGER.warning("잘못된 예약 항목을 건너뜁니다: %s", e)
        return items

    def _save_locked(self):
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)  # 쓰다 만 파일이 남지 않도록

    # ---------- 조회/수정 ----------

    def list(self):
        with self._lock:
            return [dict(item) for item in self._items]

    def add(self, raw):
        item = normalize(raw)
        with self._lock:
            self._items.append(item)
            self._items.sort(key=lambda x: (x["time"], x["id"]))
            self._save_locked()
        return dict(item)

    def update(self, schedule_id, patch):
        with self._lock:
            for i, item in enumerate(self._items):
                if item["id"] != schedule_id:
                    continue
                merged = dict(item)
                merged.update(patch or {})
                new_item = normalize(merged, existing_id=schedule_id)
                self._items[i] = new_item
                self._items.sort(key=lambda x: (x["time"], x["id"]))
                self._save_locked()
                return dict(new_item)
        return None

    def remove(self, schedule_id):
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if x["id"] != schedule_id]
            if len(self._items) == before:
                return False
            self._save_locked()
            return True


class Scheduler(threading.Thread):
    """1분 단위로 예약을 확인해서 실행한다.

    send(switch, action) 은 실제로 명령을 보내는 함수. 실패하면 예외를 던지면 된다.
    서버가 꺼져 있던 시간의 예약은 지나간 것으로 보고 실행하지 않는다.
    """

    def __init__(self, store, send, interval=15.0, now_func=datetime.now):
        super().__init__(daemon=True, name="scheduler")
        self._store = store
        self._send = send
        self._interval = interval
        self._now = now_func
        self._fired = set()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:
                _LOGGER.exception("예약 확인 중 오류")

    def tick(self):
        """한 번 확인한다. 실행한 예약 목록을 돌려준다 (테스트용)."""
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        weekday = now.weekday()

        # 날짜가 바뀌면 지난 기록은 버린다 (같은 분에 두 번 쏘는 것만 막으면 된다)
        self._fired = {k for k in self._fired if f"@{today} " in k}

        done = []
        for item in self._store.list():
            if not item["enabled"] or weekday not in item["days"]:
                continue
            if item["time"] != hhmm:
                continue
            key = f"{item['id']}@{today} {hhmm}"
            if key in self._fired:
                continue
            self._fired.add(key)
            try:
                self._send(item["switch"], item["action"])
                _LOGGER.info("예약 실행: %s %s %s",
                             item["time"], item["switch"], item["action"])
                done.append(item)
            except Exception as e:
                _LOGGER.warning("예약 실행 실패 (%s %s): %s",
                                item["time"], item["switch"], e)
        return done
