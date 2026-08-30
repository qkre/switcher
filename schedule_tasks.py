"""schedules.json 을 Windows 작업 스케줄러로 내보낸다.

왜 필요한가:
    이 PC 는 S3 절전이라 잠들면 파이썬 프로세스가 통째로 멈춘다. 서버 안의
    예약 스레드도 같이 멈추므로 자는 동안의 예약은 실행되지 않는다.
    작업 스케줄러에는 "작업을 실행하기 위해 컴퓨터의 절전 모드 해제"(WakeToRun)
    가 있어서, 예약 시각에 PC 를 깨울 수 있다.

만들어지는 작업:
    \\Switcher\\<예약id>  -  지정 요일/시각에 PC 를 깨워서
                            http://127.0.0.1:<port>/s/<switch>/<action> 을 호출한다.

    실제 명령은 웹 서버가 보내므로 web_server.py 가 떠 있어야 한다.
    (절전에서 깨어나면 서버 프로세스도 같이 되살아난다)

사용법:
    venv\\Scripts\\python.exe schedule_tasks.py --list     현재 등록된 작업 보기
    venv\\Scripts\\python.exe schedule_tasks.py --sync     schedules.json 반영
    venv\\Scripts\\python.exe schedule_tasks.py --remove   전부 삭제

주의:
    --sync 로 내보냈다면 web_server.py 는 --no-scheduler --allow-sleep 로 띄우세요.
    안 그러면 서버 안의 예약과 작업 스케줄러가 둘 다 실행돼 명령이 두 번 나갑니다.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

import schedules

TASK_FOLDER = "Switcher"

# 로그온 시 웹 서버를 띄우는 작업. 예약 작업과 같은 폴더에 있지만
# --sync 가 지우면 안 되므로 이름으로 구분한다.
AUTOSTART_ID = "_autostart"

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(PROJECT_DIR, "server.log")

# 우리 요일 번호(0=월) -> 작업 스케줄러 XML 요일 이름
XML_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]

# 작업 스케줄러가 요구하는 시작 기준 시각. 날짜는 과거 아무 날이나 되고
# 실제 실행은 요일/시각 조건으로 결정된다.
START_DATE = "2020-01-01"


def task_name(schedule_id):
    return f"\\{TASK_FOLDER}\\{schedule_id}"


def build_command(item, port, pin):
    """예약 하나를 실행할 명령줄. 창이 뜨지 않도록 PowerShell 을 숨김 실행."""
    url = f"http://127.0.0.1:{port}/s/{item['switch']}/{item['action']}"
    if pin:
        url += f"?pin={pin}"
    inner = (f"try {{ Invoke-WebRequest -UseBasicParsing -TimeoutSec 90 "
             f"-Uri '{url}' | Out-Null }} catch {{ }}")
    return "powershell.exe", (
        f'-NoProfile -NonInteractive -WindowStyle Hidden -Command "{inner}"')


def build_xml(item, port, pin, user):
    days = "".join(f"<{XML_DAYS[d]} />" for d in sorted(item["days"]))
    exe, args = build_command(item, port, pin)
    target = "둘 다" if item["switch"] == "all" else f"스위치{item['switch']}"
    what = "켜기" if item["action"] == "on" else "끄기"
    desc = (f"{item.get('label') or ''} {target} {what} "
            f"({schedules.days_text(item['days'])})").strip()

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(desc)}</Description>
    <URI>{escape(task_name(item['id']))}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{START_DATE}T{item['time']}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>{days}</DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _decode(raw):
    """schtasks 출력 디코딩.

    콘솔 코드페이지에 따라 UTF-8 이기도 하고 cp949 이기도 하다.
    실제로 이 PC 는 65001(UTF-8) 이었고, 필드명은 영어인데 값에만 한글이 섞인다.
    """
    if not raw:
        return ""
    for enc in ("utf-8", "cp949", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class Result:
    def __init__(self, proc):
        self.returncode = proc.returncode
        self.stdout = _decode(proc.stdout)
        self.stderr = _decode(proc.stderr)


def run(cmd):
    return Result(subprocess.run(cmd, capture_output=True))


def build_autostart_xml(port, user, mode):
    """로그온 시 web_server.py 를 창 없이 띄우는 작업.

    pythonw.exe 로 실행해 콘솔 창이 뜨지 않게 하고, 화면이 없으니
    --log-file 로 server.log 에 기록을 남긴다.
    """
    pythonw = os.path.join(PROJECT_DIR, "venv", "Scripts", "pythonw.exe")
    server = os.path.join(PROJECT_DIR, "web_server.py")
    extra = "--no-scheduler --allow-sleep " if mode == "sleep" else ""
    args = (f'"{server}" --port {port} {extra}'
            f'--log-file "{LOG_PATH}"')
    desc = ("로그온 시 스위처 웹 서버 자동 실행 - "
            + ("예약은 작업 스케줄러가 담당(절전 허용)"
               if mode == "sleep" else "서버가 예약 실행 + 절전 방지"))

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(desc)}</Description>
    <URI>{escape(task_name(AUTOSTART_ID))}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(PROJECT_DIR)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def register_xml(name, xml):
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        return run(["schtasks", "/Create", "/TN", name, "/XML", path, "/F"])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_install_autostart(port, user, mode):
    result = register_xml(task_name(AUTOSTART_ID),
                          build_autostart_xml(port, user, mode))
    if result.returncode != 0:
        print("자동 실행 등록 실패:")
        print("  " + (result.stderr or result.stdout).strip())
        return 1
    print(f"자동 실행 등록 완료: {task_name(AUTOSTART_ID)}")
    print(f"  로그온 30초 뒤 창 없이 web_server.py 를 띄웁니다 (포트 {port})")
    print(f"  모드: {'절전 허용 + 서버 예약 끔' if mode == 'sleep' else '절전 방지 + 서버가 예약 실행'}")
    print(f"  로그: {LOG_PATH}")
    print()
    print("지금 바로 실행해 보려면:")
    print(f'    schtasks /Run /TN "{task_name(AUTOSTART_ID)}"')
    return 0


def cmd_uninstall_autostart():
    name = task_name(AUTOSTART_ID)
    if name not in existing_tasks():
        print("자동 실행 작업이 등록돼 있지 않습니다.")
        return 0
    result = delete_task(name)
    if result.returncode == 0:
        print(f"자동 실행 작업을 삭제했습니다: {name}")
        return 0
    print(f"삭제 실패: {result.stderr.strip()}")
    return 1


def existing_tasks():
    """이미 등록된 \\Switcher\\* 작업의 이름 목록."""
    result = run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    names = []
    for line in (result.stdout or "").splitlines():
        first = line.split('","')[0].lstrip('"').strip()
        if first.startswith(f"\\{TASK_FOLDER}\\"):
            names.append(first)
    return names


def create_task(item, port, pin, user):
    return register_xml(task_name(item["id"]), build_xml(item, port, pin, user))


def delete_task(name):
    return run(["schtasks", "/Delete", "/TN", name, "/F"])


def cmd_list():
    names = existing_tasks()
    if not names:
        print("등록된 Switcher 작업이 없습니다.")
        return 0
    print(f"등록된 작업 {len(names)}건:")
    for name in names:
        result = run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"])
        info = {}
        for line in (result.stdout or "").splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
        desc = info.get("설명") or info.get("Comment") or ""
        nxt = info.get("다음 실행 시간") or info.get("Next Run Time") or "?"
        print(f"  {name}")
        print(f"      {desc}")
        print(f"      다음 실행: {nxt}")
    return 0


def cmd_sync(port, pin, user):
    store = schedules.ScheduleStore()
    items = [s for s in store.list() if s["enabled"]]
    skipped = [s for s in store.list() if not s["enabled"]]
    wanted = {task_name(s["id"]): s for s in items}

    removed = 0
    keep = {task_name(AUTOSTART_ID)}  # 자동 실행 작업은 --sync 대상이 아니다
    for name in existing_tasks():
        if name not in wanted and name not in keep:
            result = delete_task(name)
            if result.returncode == 0:
                removed += 1
            else:
                print(f"  삭제 실패 {name}: {result.stderr.strip()}")

    created, failed = 0, 0
    for name, item in wanted.items():
        result = create_task(item, port, pin, user)
        target = "둘 다" if item["switch"] == "all" else f"스위치{item['switch']}"
        what = "켜기" if item["action"] == "on" else "끄기"
        label = (f"{item['time']}  {target} {what}  "
                 f"({schedules.days_text(item['days'])})")
        if result.returncode == 0:
            print(f"  등록  {label}")
            created += 1
        else:
            print(f"  실패  {label}")
            print(f"        {(result.stderr or result.stdout).strip()}")
            failed += 1

    print()
    print(f"등록/갱신 {created}건, 삭제 {removed}건, 실패 {failed}건")
    if skipped:
        print(f"꺼둔 예약 {len(skipped)}건은 작업을 만들지 않았습니다.")
    if created:
        print()
        print("서버는 아래처럼 띄우세요 (예약 중복 실행 방지 + 절전 허용):")
        print("    venv\\Scripts\\python.exe web_server.py --no-scheduler --allow-sleep")
    return 1 if failed else 0


def cmd_remove():
    names = existing_tasks()
    if not names:
        print("삭제할 작업이 없습니다.")
        return 0
    ok = 0
    for name in names:
        result = delete_task(name)
        if result.returncode == 0:
            print(f"  삭제  {name}")
            ok += 1
        else:
            print(f"  실패  {name}: {result.stderr.strip()}")
    print(f"\n{ok}/{len(names)}건 삭제")
    return 0 if ok == len(names) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="schedules.json 을 Windows 작업 스케줄러로 내보낸다")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="등록된 작업 보기")
    group.add_argument("--sync", action="store_true", help="schedules.json 반영")
    group.add_argument("--remove", action="store_true", help="예약 작업 전부 삭제")
    group.add_argument("--install-autostart", action="store_true",
                       help="로그온 시 web_server.py 자동 실행 등록")
    group.add_argument("--uninstall-autostart", action="store_true",
                       help="자동 실행 등록 해제")
    parser.add_argument("--port", type=int, default=8000,
                        help="웹 서버 포트 (기본 8000)")
    parser.add_argument("--pin", default=os.environ.get("SWITCHER_WEB_PIN", ""),
                        help="서버에 PIN 을 걸었다면 지정")
    parser.add_argument("--mode", choices=("sleep", "awake"), default="sleep",
                        help="자동 실행 모드. sleep=절전 허용(작업 스케줄러가 예약 담당), "
                             "awake=절전 방지(서버가 예약 실행)")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("Windows 에서만 동작합니다.")
        return 1

    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"

    if args.list:
        return cmd_list()
    if args.remove:
        return cmd_remove()
    if args.install_autostart:
        return cmd_install_autostart(args.port, user, args.mode)
    if args.uninstall_autostart:
        return cmd_uninstall_autostart()
    return cmd_sync(args.port, args.pin.strip(), user)


if __name__ == "__main__":
    sys.exit(main())
