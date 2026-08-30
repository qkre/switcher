import tkinter as tk
import time
import test_support
from gui import SwitchApp

test_support.use_temp_config()  # 실제 config.json 을 건드리지 않는다


class FailingLink:
    """send 가 항상 실패하는 SwitcherLink 대역."""
    connected = False

    def send(self, *a, **k):
        raise RuntimeError('simulated connect failure')

    def disconnect(self):
        pass

    def close(self):
        pass


# 실패 대화상자가 뜨면 테스트가 멈추므로 messagebox 를 막는다
import gui
shown = []
gui.messagebox.showerror = lambda title, msg: shown.append((title, msg))

root = tk.Tk()
app = SwitchApp(root)
app._link.close()
app._link = FailingLink()

app.mac_var.set('00:11:22:33:44:55')
app.on_action(1, 'on')

start = time.time()
while time.time() - start < 1.0:
    root.update()
    time.sleep(0.05)

status = app.status_label.cget('text')
print('status label:', status)
print('dialog:', shown)

assert 'RuntimeError: simulated connect failure' in status, status
assert shown and 'simulated connect failure' in shown[0][1], shown
assert app.switch1_on_btn.cget('state') == 'normal', "실패해도 컨트롤은 풀려야 한다"
print('OK')

root.destroy()
