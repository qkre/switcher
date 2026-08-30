import asyncio
import json
import logging
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox

from bleak import BleakScanner

import switcher_link

# SWITCHER_CONFIG 환경변수로 설정 파일 위치를 바꿀 수 있다 (테스트용)
CONFIG_PATH = os.environ.get("SWITCHER_CONFIG") or os.path.join(
    os.path.dirname(__file__), "config.json")

# android/flutter_app/lib/main.dart 의 SERVICE_UUID 와 동일
SWITCHER_SERVICE_UUID = "0000150b-0000-1000-8000-00805f9b34fb"
SWITCHER_NAME_HINT = "switcher"
SCAN_TIMEOUT = 15.0


class GuiLogHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        # Call the GUI callback in a thread-safe way
        self.callback(msg)


class SwitchApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PySwitcherIO GUI")

        # 백그라운드 스레드 -> UI 전달용 큐 (로그 핸들러보다 먼저 만들어야 한다).
        # tkinter 의 after() 는 메인 스레드에서만 호출해야 안전하므로,
        # 다른 스레드는 큐에 넣고 메인 스레드가 주기적으로 꺼내 실행한다.
        self._ui_queue = queue.Queue()

        # load config
        self.config = self._load_config()
        self.mac_var = tk.StringVar(value=self.config.get("mac", ""))
        self.type_var = tk.IntVar(value=self.config.get("type", 2))  # 1 or 2
        self.invert_var = tk.IntVar(value=1 if self.config.get("invert", False) else 0)
        self.keepalive_var = tk.IntVar(
            value=1 if self.config.get("keep_alive", True) else 0)

        # BLE 연결을 물고 있는 계층 (전용 스레드 + 이벤트 루프)
        self._link = switcher_link.SwitcherLink()

        # UI
        frame = tk.Frame(root, padx=10, pady=10)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="스위처 MAC 주소:").grid(row=0, column=0, sticky="w")
        self.mac_entry = tk.Entry(frame, textvariable=self.mac_var, width=30)
        self.mac_entry.grid(row=0, column=1, sticky="w")
        tk.Button(frame, text="스위처 저장", command=self.save_mac).grid(row=0, column=2, padx=5)
        self.find_devices_btn = tk.Button(frame, text="스위처 찾기", command=self.find_devices)
        self.find_devices_btn.grid(row=0, column=3, padx=5)

        self.type_check = tk.Checkbutton(frame, text="2구 스위처 (체크: 2구, 해제: 1구)", variable=self.type_var, onvalue=2, offvalue=1, command=self.on_type_change)
        self.type_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.invert_check = tk.Checkbutton(frame, text="ON/OFF 반대로 작동", variable=self.invert_var, command=self.on_invert_change)
        self.invert_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.keepalive_check = tk.Checkbutton(
            frame, text="연결 유지 (끄면 명령마다 연결/해제)",
            variable=self.keepalive_var, command=self.on_keepalive_change)
        self.keepalive_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 10))

        # Switch controls
        # Replace checkboxes with independent ON/OFF buttons (always available to press)
        self.switch1_on_btn = tk.Button(frame, text="스위치1 ON", width=12, command=lambda: self.on_action(1, "on"))
        self.switch1_on_btn.grid(row=4, column=0, sticky="w")
        self.switch1_off_btn = tk.Button(frame, text="스위치1 OFF", width=12, command=lambda: self.on_action(1, "off"))
        self.switch1_off_btn.grid(row=4, column=1, sticky="w")

        self.switch2_on_btn = tk.Button(frame, text="스위치2 ON", width=12, command=lambda: self.on_action(2, "on"))
        self.switch2_on_btn.grid(row=4, column=2, sticky="w")
        self.switch2_off_btn = tk.Button(frame, text="스위치2 OFF", width=12, command=lambda: self.on_action(2, "off"))
        self.switch2_off_btn.grid(row=4, column=3, sticky="w")

        # Initially hide switch2 buttons if type is 1
        if self.type_var.get() == 1:
            self.switch2_on_btn.grid_remove()
            self.switch2_off_btn.grid_remove()

        self.status_label = tk.Label(frame, text="", fg="blue")
        self.status_label.grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))

        # Logging setup
        self.log_handler = GuiLogHandler(self.on_log_message)
        self.log_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(message)s")
        self.log_handler.setFormatter(formatter)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

        # Keep a reference to ioswitcher object per current operation
        self._operation_thread = None

        self._pump_ui_queue()

        # Save config on close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _post_to_ui(self, fn):
        """어느 스레드에서든 안전하게 UI 콜백을 예약한다."""
        try:
            self._ui_queue.put(fn)
        except Exception:
            pass

    def _pump_ui_queue(self):
        """메인 스레드에서만 실행된다. 큐에 쌓인 UI 콜백을 처리하고 자신을 재예약."""
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                # 여기서 logging 을 쓰면 GuiLogHandler 를 통해 되먹임이 생긴다
                traceback.print_exc()
        try:
            self.root.after(50, self._pump_ui_queue)
        except Exception:
            pass  # root 가 이미 destroy 된 경우

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:  # BOM 허용
                return json.load(f)
        except Exception:
            return {}

    def save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "mac": self.mac_var.get(),
                    "type": self.type_var.get(),
                    "invert": bool(self.invert_var.get()),
                    "keep_alive": bool(self.keepalive_var.get()),
                }, f)
        except Exception as e:
            print("Failed to save config:", e)

    def save_mac(self):
        # Just save current config to disk
        self.save_config()
        self.status_label.config(text="MAC saved.")

    def on_type_change(self):
        if self.type_var.get() == 1:
            self.switch2_on_btn.grid_remove()
            self.switch2_off_btn.grid_remove()
        else:
            self.switch2_on_btn.grid()
            self.switch2_off_btn.grid()
        self.save_config()

    def on_invert_change(self):
        self.save_config()

    def find_devices(self):
        # Open a scanning window and start scanning in background
        if getattr(self, '_scan_window', None):
            return
        self._scan_all_results = []
        self._scan_window = tk.Toplevel(self.root)
        self._scan_window.title('블루투스 장치 선택')
        self._scan_window.geometry('560x460')
        self._scan_window.transient(self.root)
        self._scan_window.grab_set()

        lbl = tk.Label(self._scan_window, text=f"블루투스 장치를 찾는 중... ({int(SCAN_TIMEOUT)}초)")
        lbl.pack(anchor='w', padx=8, pady=(8, 0))
        self._scan_status_label = lbl

        # "스위처만 보기" 토글 (결과 프레임 바깥에 둔다)
        opt_frame = tk.Frame(self._scan_window)
        opt_frame.pack(fill=tk.X, padx=8, pady=(4, 0))
        self._scan_only_switcher = tk.IntVar(value=1)
        tk.Checkbutton(opt_frame, text='스위처로 보이는 것만 보기',
                       variable=self._scan_only_switcher,
                       command=self._render_scan_rows).pack(side=tk.LEFT)

        # 스크롤 가능한 결과 영역
        list_container = tk.Frame(self._scan_window)
        list_container.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        canvas = tk.Canvas(list_container, highlightthickness=0)
        scrollbar = tk.Scrollbar(list_container, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._scan_results_frame = tk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=self._scan_results_frame, anchor='nw')
        self._scan_results_frame.bind(
            '<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.bind_all('<MouseWheel>',
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), 'units'))
        self._scan_canvas = canvas

        btn_frame = tk.Frame(self._scan_window)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Button(btn_frame, text='Close', command=self._close_scan_window).pack(side=tk.RIGHT)
        self._rescan_btn = tk.Button(btn_frame, text='다시 검색', command=self._start_scan_thread)
        self._rescan_btn.pack(side=tk.RIGHT, padx=4)

        self._start_scan_thread()

    def _start_scan_thread(self):
        if not getattr(self, '_scan_window', None):
            return
        try:
            self._scan_status_label.config(
                text=f"블루투스 장치를 찾는 중... ({int(SCAN_TIMEOUT)}초)")
            self._rescan_btn.config(state=tk.DISABLED)
        except Exception:
            pass
        for c in self._scan_results_frame.winfo_children():
            c.destroy()
        try:
            self.find_devices_btn.config(state=tk.DISABLED)
        except Exception:
            pass
        t = threading.Thread(target=self._do_scan, args=(self._scan_status_label,))
        t.daemon = True
        t.start()

    def _close_scan_window(self):
        if getattr(self, '_scan_window', None):
            try:
                self._scan_window.grab_release()
            except Exception:
                pass
            try:
                self._scan_window.unbind_all('<MouseWheel>')
            except Exception:
                pass
            self._scan_window.destroy()
            self._scan_window = None
        try:
            self.find_devices_btn.config(state=tk.NORMAL)
        except Exception:
            pass

    def _do_scan(self, status_label):
        try:
            entries = self._discover_devices()
        except Exception as e:
            err = f"Scan error: {e}"
            self._post_to_ui(lambda: self._show_scan_error(err))
            return
        for d in entries:
            d['is_switcher'] = self._is_switcher(d['name'], d['uuids'])
        # 스위처 의심 장치를 위로, 그다음 신호 강한 순
        entries.sort(key=lambda d: (not d['is_switcher'],
                                    -(d['rssi'] if d['rssi'] is not None else -999)))
        self._post_to_ui(lambda: self._show_scan_results(entries))

    @staticmethod
    def _discover_devices():
        """BLE 스캔 결과를 [{name, address, uuids, rssi}] 로 정규화해서 반환."""
        try:
            maybe = BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
        except TypeError:
            # return_adv 를 지원하지 않는 스캐너(테스트용 가짜 스캐너 등)
            maybe = BleakScanner.discover(timeout=SCAN_TIMEOUT)
        raw = asyncio.run(maybe) if asyncio.iscoroutine(maybe) else maybe

        entries = []
        if isinstance(raw, dict):
            # {address: (BLEDevice, AdvertisementData)}
            for addr, (device, adv) in raw.items():
                entries.append({
                    'name': device.name or getattr(adv, 'local_name', None),
                    'address': getattr(device, 'address', addr),
                    'uuids': {u.lower() for u in (getattr(adv, 'service_uuids', None) or [])},
                    'rssi': getattr(adv, 'rssi', None),
                })
        else:
            # [BLEDevice] - 광고 데이터가 없으므로 이름으로만 판단 가능
            for d in raw:
                entries.append({
                    'name': d.name,
                    'address': d.address,
                    'uuids': set(),
                    'rssi': getattr(d, 'rssi', None),
                })
        return entries

    @staticmethod
    def _is_switcher(name, uuids):
        # 이름은 광고 패킷마다 빠질 수 있으므로 서비스 UUID 도 함께 본다
        if SWITCHER_SERVICE_UUID in (uuids or set()):
            return True
        return bool(name) and SWITCHER_NAME_HINT in name.lower()

    def _show_scan_error(self, msg: str):
        # 창 전체를 지우지 않고 결과 영역에만 표시해서 '다시 검색'을 계속 쓸 수 있게 한다
        if getattr(self, '_scan_window', None):
            frame = self._scan_results_frame
            for c in frame.winfo_children():
                c.destroy()
            tk.Label(frame, text=msg, fg='red', justify='left', wraplength=480).pack(
                anchor='w', pady=(0, 6))
            tk.Label(frame, fg='gray30', justify='left', wraplength=480,
                     text="블루투스가 꺼져 있거나, 어댑터가 없거나, 권한이 없을 수 있습니다.").pack(anchor='w')
            try:
                self._scan_status_label.config(text="스캔 실패")
            except Exception:
                pass
        try:
            self._rescan_btn.config(state=tk.NORMAL)
        except Exception:
            pass
        try:
            self.find_devices_btn.config(state=tk.NORMAL)
        except Exception:
            pass

    @staticmethod
    def _normalize_entry(item):
        """(name, addr) 튜플과 dict 를 모두 받아 dict 로 통일."""
        if isinstance(item, dict):
            d = dict(item)
        else:
            name, addr = item[0], item[1]
            d = {'name': name, 'address': addr, 'uuids': set(), 'rssi': None}
        d.setdefault('uuids', set())
        d.setdefault('rssi', None)
        if 'is_switcher' not in d:
            d['is_switcher'] = SwitchApp._is_switcher(d.get('name'), d.get('uuids'))
        return d

    def _show_scan_results(self, results):
        if not getattr(self, '_scan_window', None):
            return
        self._scan_all_results = [self._normalize_entry(r) for r in (results or [])]
        self._render_scan_rows()
        try:
            self._rescan_btn.config(state=tk.NORMAL)
        except Exception:
            pass
        try:
            self.find_devices_btn.config(state=tk.NORMAL)
        except Exception:
            pass

    def _render_scan_rows(self):
        if not getattr(self, '_scan_window', None):
            return
        frame = self._scan_results_frame
        for c in frame.winfo_children():
            c.destroy()

        all_results = getattr(self, '_scan_all_results', [])
        only_switcher = bool(getattr(self, '_scan_only_switcher', None)
                             and self._scan_only_switcher.get())
        shown = [d for d in all_results if d['is_switcher']] if only_switcher else all_results

        hit_count = sum(1 for d in all_results if d['is_switcher'])
        try:
            self._scan_status_label.config(
                text=f"장치 {len(all_results)}개 발견 (스위처 의심 {hit_count}개) — "
                     f"사용할 장치의 '선택'을 누르세요")
        except Exception:
            pass

        if not shown:
            if not all_results:
                msg = "블루투스 장치를 하나도 찾지 못했습니다. 블루투스가 켜져 있는지 확인하세요."
            else:
                msg = ("스위처로 보이는 장치가 없습니다.\n"
                       "'스위처로 보이는 것만 보기'를 해제하면 전체 목록에서 직접 고를 수 있습니다.")
            tk.Label(frame, text=msg, fg='gray30', justify='left').pack(anchor='w')
            return

        for d in shown:
            name, addr = d.get('name'), d.get('address')
            row = tk.Frame(frame)
            row.pack(fill=tk.X, pady=1)
            if d['is_switcher']:
                row.config(bg='#e8f4ff')
            label_text = ('★ ' if d['is_switcher'] else '   ') + (name or '<이름 없음>')
            name_lbl = tk.Label(row, text=label_text, width=24, anchor='w')
            addr_lbl = tk.Label(row, text=addr, width=19, anchor='w')
            rssi_lbl = tk.Label(row, text=('' if d['rssi'] is None else f"{d['rssi']}dBm"),
                                width=8, anchor='w')
            for w in (name_lbl, addr_lbl, rssi_lbl):
                if d['is_switcher']:
                    w.config(bg='#e8f4ff')
                w.pack(side=tk.LEFT)
            tk.Button(row, text='Copy',
                      command=lambda a=addr: self._copy_to_clipboard(a)).pack(side=tk.RIGHT, padx=4)
            # 'Use' 텍스트는 기존 테스트가 참조하므로 유지한다
            tk.Button(row, text='Use',
                      command=lambda a=addr: self._use_mac(a)).pack(side=tk.RIGHT)

    def _copy_to_clipboard(self, addr: str):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(addr)
        except Exception:
            pass

    def _use_mac(self, addr: str):
        self.mac_var.set(addr)
        self.save_config()
        self.status_label.config(text=f"MAC set to {addr}")
        self._close_scan_window()

    def on_log_message(self, message: str):
        # This may be called from non-main threads; schedule into mainloop
        self._post_to_ui(lambda: self._handle_log_message(message))

    def _handle_log_message(self, message: str):
        # 라이브러리/연결 계층의 최신 로그를 상태줄에 보여준다
        self.status_label.config(text=message)

    def _on_operation_success(self):
        self._enable_controls()
        state = "연결 유지 중" if self._link.connected else "연결 해제됨"
        self.status_label.config(text=f"전송 완료 ({state})")

    def _on_operation_failed(self, reason: str):
        self._enable_controls()
        self.status_label.config(text=f"실패: {reason}")
        # 원인을 같이 보여줘야 MAC 잘못 넣은 경우를 알아챌 수 있다
        messagebox.showerror("Operation Failed", "스위쳐 통신 실패..\n\n" + reason)

    def _disable_controls(self):
        self.switch1_on_btn.config(state=tk.DISABLED)
        self.switch1_off_btn.config(state=tk.DISABLED)
        self.switch2_on_btn.config(state=tk.DISABLED)
        self.switch2_off_btn.config(state=tk.DISABLED)
        self.mac_entry.config(state=tk.DISABLED)
        self.type_check.config(state=tk.DISABLED)
        self.invert_check.config(state=tk.DISABLED)
        self.keepalive_check.config(state=tk.DISABLED)

    def _enable_controls(self):
        self.switch1_on_btn.config(state=tk.NORMAL)
        self.switch1_off_btn.config(state=tk.NORMAL)
        if self.type_var.get() == 2:
            self.switch2_on_btn.config(state=tk.NORMAL)
            self.switch2_off_btn.config(state=tk.NORMAL)
        self.mac_entry.config(state=tk.NORMAL)
        self.type_check.config(state=tk.NORMAL)
        self.invert_check.config(state=tk.NORMAL)
        self.keepalive_check.config(state=tk.NORMAL)

    def on_action(self, switch_index: int, action: str):
        mac = self.mac_var.get().strip()
        if not mac:
            messagebox.showwarning("No MAC", "MAC 주소를 입력하고 저장하세요.")
            return
        invert = bool(self.invert_var.get())
        # Apply invert setting: if invert then flip the requested action
        if invert:
            action = "off" if action == "on" else "on"
        self._disable_controls()
        self.status_label.config(text=f"스위치{switch_index} {action.upper()} 전송 중...")
        # tk 변수는 메인 스레드에서만 읽을 수 있으므로 여기서 값을 뽑아 넘긴다
        keep_alive = bool(self.keepalive_var.get())
        t = threading.Thread(
            target=self._run_switch_command,
            args=(mac, self.type_var.get(), switch_index, action, keep_alive))
        t.daemon = True
        t.start()
        self._operation_thread = t

    def _run_switch_command(self, mac: str, type_num: int, switch_index: int,
                            action: str, keep_alive: bool):
        if switch_index == 2 and type_num == 1:
            # UI 가 1구일 때 스위치2 를 숨기지만 혹시 모르니 막는다
            msg = "Device configured as 1구인데 Switch 2를 작동하려고 했습니다."
            self._post_to_ui(lambda msg=msg: self._on_operation_failed(msg))
            return
        channel_type = 1 if switch_index == 1 else 2
        try:
            self._link.send(mac, channel_type, action, keep_alive=keep_alive)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self._post_to_ui(lambda msg=msg: self._on_operation_failed(msg))
            return
        self._post_to_ui(self._on_operation_success)

    def on_keepalive_change(self):
        """'연결 유지' 를 끄면 지금 잡고 있는 연결을 바로 놓아준다."""
        self.save_config()
        if not self.keepalive_var.get():
            threading.Thread(target=self._link.disconnect, daemon=True).start()
            self.status_label.config(text="연결 유지 해제 — 연결을 끊습니다.")
        else:
            self.status_label.config(text="연결 유지 켜짐.")

    def on_close(self):
        self.save_config()
        try:
            self._link.close()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = SwitchApp(root)
    root.mainloop()
