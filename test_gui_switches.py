import tkinter as tk
import time
import test_support
from gui import SwitchApp

test_support.use_temp_config()  # 실제 config.json 을 건드리지 않는다

results = []


class DummyLink:
    """SwitcherLink 대역. 실제 BLE 없이 호출 내용만 기록한다."""
    connected = True

    def send(self, mac, switch_index, action, keep_alive=True):
        results.append(('send', mac, switch_index, action, keep_alive))
        return True

    def disconnect(self):
        results.append(('disconnect',))

    def close(self):
        results.append(('close',))


root = tk.Tk()
app = SwitchApp(root)
app._link.close()          # __init__ 이 만든 진짜 링크는 정리하고
app._link = DummyLink()    # 대역으로 교체
results.clear()

app.mac_var.set('00:11:22:33:44:55')
# set device config to 2-gang so both switches visible
app.type_var.set(2)
app.on_type_change()


def pump(seconds=1.0):
    start = time.time()
    while time.time() - start < seconds:
        root.update()
        time.sleep(0.01)


# Press Switch1 ON
app.on_action(1, 'on')
pump()

# Press Switch2 ON
app.on_action(2, 'on')
pump()

# 연결 유지를 끄면 keep_alive=False 로 전달되고 즉시 끊어야 한다
app.keepalive_var.set(0)
app.on_keepalive_change()
app.on_action(1, 'off')
pump()

print('results:', results)

expected = [
    ('send', '00:11:22:33:44:55', 1, 'on', True),
    ('send', '00:11:22:33:44:55', 2, 'on', True),
    ('disconnect',),
    ('send', '00:11:22:33:44:55', 1, 'off', False),
]
assert results == expected, f"\nexpected: {expected}\nactual  : {results}"
assert app.switch1_on_btn.cget('state') == 'normal', "컨트롤이 다시 활성화되어야 한다"
print('OK')

root.destroy()
