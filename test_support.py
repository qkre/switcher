"""테스트용 공통 헬퍼.

GUI 테스트는 SwitchApp 을 만들 때 config 를 읽고, 조작할 때마다 저장한다.
그대로 두면 실제 config.json 의 MAC 주소가 테스트 더미값으로 덮어써지므로,
각 테스트는 SwitchApp 을 만들기 전에 use_temp_config() 를 호출해야 한다.
"""
import atexit
import json
import os
import tempfile

import gui

_ORIGINAL_CONFIG_PATH = gui.CONFIG_PATH


def use_temp_config(initial=None):
    """gui.CONFIG_PATH 를 임시 파일로 돌리고, 종료 시 원복 + 삭제한다.

    initial 이 None 이면 파일이 없는 상태(=기본 설정)로 시작한다.
    """
    fd, path = tempfile.mkstemp(prefix="switcher_test_config_", suffix=".json")
    os.close(fd)
    if initial is None:
        os.remove(path)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(initial, f)

    gui.CONFIG_PATH = path

    def _cleanup():
        gui.CONFIG_PATH = _ORIGINAL_CONFIG_PATH
        try:
            os.remove(path)
        except OSError:
            pass

    atexit.register(_cleanup)
    return path
