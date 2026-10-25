"""
Unisend for Android (Kivy app).

Same sharing engine as the Windows version (unisend_core.py): other people open
a link in their browser, and other Unisend apps on the same Wi-Fi show up in the
"Nearby devices" list so you can send files straight to them.
"""
import os
import socket
import threading
import types
import webbrowser

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp, sp
from kivy.properties import ListProperty
from kivy.uix.anchorlayout import AnchorLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform

import unisend_core as core

ANDROID = platform == "android"

BG = (0.059, 0.09, 0.165, 1)
CARD = (0.118, 0.161, 0.231, 1)
FG = (0.89, 0.91, 0.94, 1)
MUT = (0.58, 0.64, 0.72, 1)
ACC = (0.22, 0.74, 0.97, 1)
BTN = (0.2, 0.28, 0.41, 1)
BTN_ON = (0.13, 0.45, 0.65, 1)
WARN = (0.98, 0.75, 0.14, 1)

if ANDROID:
    from jnius import autoclass
    from android.runnable import run_on_ui_thread

    @run_on_ui_thread
    def set_keep_screen_on(on):
        try:
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            flag = autoclass("android.view.WindowManager$LayoutParams").FLAG_KEEP_SCREEN_ON
            window = activity.getWindow()
            if on:
                window.addFlags(flag)
            else:
                window.clearFlags(flag)
        except Exception:
            pass
else:
    def set_keep_screen_on(on):
        pass


# ------------------------------------------------------------ Android bits ---
def android_activity():
    return autoclass("org.kivy.android.PythonActivity").mActivity


def android_ip():
    """Find this phone's Wi-Fi (or hotspot) address."""
    try:
        NI = autoclass("java.net.NetworkInterface")
        fallback = None
        nets = NI.getNetworkInterfaces()
        while nets.hasMoreElements():
            ni = nets.nextElement()
            if ni.isLoopback() or not ni.isUp():
                continue
            name = str(ni.getName())
            addrs = ni.getInetAddresses()
            while addrs.hasMoreElements():
                a = addrs.nextElement()
                host = str(a.getHostAddress())
                if ":" in host or a.isLoopbackAddress():
                    continue
                if name.startswith(("wlan", "ap", "swlan", "eth")):
                    return host
                fallback = fallback or host
        return fallback
    except Exception:
        return None


_core_ip = core.get_local_ip


def get_ip():
    if ANDROID:
        ip = android_ip()
        if ip:
            return ip
    return _core_ip()


core.get_local_ip = get_ip   # the "nearby devices" announcer uses this too


def has_all_files():
    """On Android 11+ sharing a normal folder needs 'All files access'."""
    if not ANDROID:
        return True
    try:
        if autoclass("android.os.Build$VERSION").SDK_INT >= 30:
            return bool(autoclass("android.os.Environment").isExternalStorageManager())
    except Exception:
        pass
    return True


def open_all_files_settings():
    try:
        activity = android_activity()
        Intent = autoclass("android.content.Intent")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        intent.setData(Uri.parse("package:" + activity.getPackageName()))
        activity.startActivity(intent)
    except Exception:
        pass


def open_url(url):
    if ANDROID:
        try:
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            android_activity().startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            return
        except Exception:
            pass
    webbrowser.open(url)


def can_use(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".unisend_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def preferred_dir():
    if ANDROID:
        return "/storage/emulated/0/Unisend"
    return os.path.join(os.path.expanduser("~"), "UnisendShare")


def fallback_dir():
    if ANDROID:
        try:
            base = android_activity().getExternalFilesDir(None).getAbsolutePath()
            return os.path.join(str(base), "Unisend")
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "UnisendShare")


def device_name():
    if ANDROID:
        try:
            return str(autoclass("android.os.Build").MODEL)[:40]
        except Exception:
            pass
    return (socket.gethostname() or "Unisend device")[:40]


def acquire_locks():
    """Keep Wi-Fi awake and let the phone receive 'nearby device' broadcasts."""
    held = []
    if not ANDROID:
        return held
    try:
        activity = android_activity()
        Context = autoclass("android.content.Context")
        pm = activity.getSystemService(Context.POWER_SERVICE)
        PM = autoclass("android.os.PowerManager")
        wl = pm.newWakeLock(PM.PARTIAL_WAKE_LOCK, "unisend:cpu")
        wl.acquire()
        held.append(wl)
    except Exception:
        pass
    try:
        wifi = android_activity().getSystemService(autoclass("android.content.Context").WIFI_SERVICE)
        try:
            wlock = wifi.createWifiLock(3, "unisend:wifi")   # full high-performance
            wlock.acquire()
            held.append(wlock)
        except Exception:
            pass
        mc = wifi.createMulticastLock("unisend:multicast")
        mc.acquire()
        held.append(mc)
    except Exception:
        pass
    return held


def make_matrix(url):
    try:
        import qrcode
        q = qrcode.QRCode(border=0)
        q.add_data(url)
        q.make(fit=True)
        return [[bool(c) for c in row] for row in q.get_matrix()]
    except Exception:
        return []


# ----------------------------------------------------------------- widgets ---
class QRView(Widget):
    matrix = ListProperty([])

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self.redraw, size=self.redraw, matrix=self.redraw)

    def redraw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(1, 1, 1, 1)
            Rectangle(pos=self.pos, size=self.size)
            n = len(self.matrix)
            if not n:
                return
            cell = min(self.width, self.height) / (n + 4)
            ox = self.x + (self.width - cell * n) / 2
            oy = self.y + (self.height - cell * n) / 2
            Color(0, 0, 0, 1)
            for r, row in enumerate(self.matrix):
                for c, on in enumerate(row):
                    if on:
                        Rectangle(pos=(ox + c * cell, oy + (n - 1 - r) * cell),
                                  size=(cell + 0.5, cell + 0.5))


class Card(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", size_hint_y=None,
                         padding=dp(12), spacing=dp(8), **kw)
        self.bind(minimum_height=self.setter("height"))
        with self.canvas.before:
            Color(*CARD)
            self._bg = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size


def wlabel(text="", color=FG, size=14, bold=False):
    lab = Label(text=text, color=color, font_size=sp(size), bold=bold,
                size_hint_y=None, halign="left", valign="middle")
    lab.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
    lab.bind(texture_size=lambda inst, s: setattr(inst, "height", s[1] + dp(4)))
    return lab


def button(text, on_release, color=BTN):
    b = Button(text=text, size_hint_y=None, height=dp(46), background_normal="",
               background_color=color, color=FG, font_size=sp(15))
    b.bind(on_release=on_release)
    return b


# --------------------------------------------------------------------- app ---
class UnisendApp(App):
    title = "Unisend"

    def build(self):
        Window.clearcolor = BG
        self.sel = None
        self._peer_key = None
        self.ticks = 0
        self.msg = {"t": ""}
        self.locks = []
        self.fatal = ""

        # Folder to share: the proper one if we are allowed, otherwise the app's own folder
        folder = preferred_dir() if can_use(preferred_dir()) else fallback_dir()
        can_use(folder)
        self.args = types.SimpleNamespace(
            dir=os.path.realpath(folder), user=None, password=None, ftp_port=2121,
            http_port=8080, use_ftp=False, no_ftp=True, name=device_name(),
            discovery_addr="255.255.255.255")
        self.ip = get_ip()

        self.build_ui()
        self.start_services()
        return self.root_view

    # ---------------------------------------------------------------- UI ---
    def build_ui(self):
        scroll = ScrollView(do_scroll_x=False, bar_width=dp(3))
        col = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(12),
                        spacing=dp(12))
        col.bind(minimum_height=col.setter("height"))
        scroll.add_widget(col)
        self.root_view = scroll

        col.add_widget(wlabel("Unisend", ACC, 26, True))
        col.add_widget(wlabel(f"This device: {self.args.name}", MUT, 13))

        # --- link + QR
        c = Card()
        c.add_widget(wlabel("Tell the other person to open this link in their browser:", MUT, 13))
        self.url_box = TextInput(text="", readonly=True, multiline=False, size_hint_y=None,
                                 height=dp(46), font_size=sp(16), background_color=(0.06, 0.09, 0.16, 1),
                                 foreground_color=FG, cursor_color=FG)
        c.add_widget(self.url_box)
        self.warn = wlabel("", WARN, 13)
        c.add_widget(self.warn)
        holder = AnchorLayout(size_hint_y=None, height=dp(230))
        self.qr = QRView(size_hint=(None, None), size=(dp(220), dp(220)))
        holder.add_widget(self.qr)
        c.add_widget(holder)
        c.add_widget(wlabel("Or scan this with their phone camera. Both must be on the same Wi-Fi.", MUT, 12))
        col.add_widget(c)

        # --- folder
        c = Card()
        c.add_widget(wlabel("Folder you are sharing:", MUT, 13))
        self.dir_lbl = wlabel(self.args.dir, FG, 14)
        c.add_widget(self.dir_lbl)
        self.perm_lbl = wlabel("", WARN, 12)
        c.add_widget(self.perm_lbl)
        c.add_widget(button("Change folder", lambda *_: self.pick_folder()))
        col.add_widget(c)

        # --- speeds
        c = Card()
        self.speed_lbl = wlabel("", ACC, 18, True)
        c.add_widget(self.speed_lbl)
        self.total_lbl = wlabel("", MUT, 12)
        c.add_widget(self.total_lbl)
        col.add_widget(c)

        # --- nearby devices
        c = Card()
        c.add_widget(wlabel("Nearby devices running Unisend", MUT, 13))
        self.peer_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.peer_box.bind(minimum_height=self.peer_box.setter("height"))
        c.add_widget(self.peer_box)
        c.add_widget(button("Send files to selected...", lambda *_: self.on_send(), BTN_ON))
        c.add_widget(button("Browse their files", lambda *_: self.on_browse()))
        self.status_lbl = wlabel("", MUT, 13)
        c.add_widget(self.status_lbl)
        col.add_widget(c)

        # --- transfers
        c = Card()
        c.add_widget(wlabel("Transfers", MUT, 13))
        self.tr_lbl = wlabel("No transfers yet", FG, 13)
        c.add_widget(self.tr_lbl)
        col.add_widget(c)

        # --- keep awake
        c = Card()
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(10))
        row.add_widget(wlabel("Keep screen on while sharing", FG, 14))
        sw = Switch(active=True, size_hint_x=None, width=dp(80))
        sw.bind(active=lambda _i, v: set_keep_screen_on(v))
        row.add_widget(sw)
        c.add_widget(row)
        c.add_widget(wlabel("Keep Unisend open while sharing. For best results, set the "
                            "phone's battery setting for Unisend to 'Unrestricted'.", MUT, 12))
        col.add_widget(c)
        set_keep_screen_on(True)

        self.update_address()

    def update_address(self):
        url = f"http://{self.ip}:{self.args.http_port}"
        self.url_box.text = url
        self.qr.matrix = make_matrix(url)

    # ---------------------------------------------------------- services ---
    def start_services(self):
        threading.Thread(target=core.sampler, daemon=True).start()
        started = False
        for port in range(8080, 8090):
            self.args.http_port = port
            try:
                core.start_web(self.args)
                started = True
                break
            except OSError:
                continue
        if not started:
            self.fatal = "Could not open a network port. Close other apps and restart Unisend."
        core.start_discovery(self.args)
        self.locks = acquire_locks()
        self.update_address()
        Clock.schedule_interval(self.tick, 0.5)

    def on_start(self):
        if ANDROID:
            try:
                from android.permissions import Permission, request_permissions
                request_permissions([Permission.READ_EXTERNAL_STORAGE,
                                     Permission.WRITE_EXTERNAL_STORAGE])
            except Exception:
                pass
            Clock.schedule_once(lambda dt: self.check_storage_access(), 1.5)

    def on_pause(self):
        return True

    def on_resume(self):
        self.upgrade_folder_if_possible()

    def check_storage_access(self):
        if has_all_files():
            self.upgrade_folder_if_possible()
            return
        body = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        body.add_widget(wlabel("To share a normal folder (like Downloads or Pictures), Android needs "
                               "you to allow 'All files access' for Unisend. Without it, Unisend "
                               "shares a private folder that only it can see.", FG, 14))
        pop = Popup(title="Allow file access", content=body, size_hint=(0.92, None), height=dp(380))
        body.add_widget(button("Open settings", lambda *_: (pop.dismiss(), open_all_files_settings()), BTN_ON))
        body.add_widget(button("Not now", lambda *_: pop.dismiss()))
        pop.open()

    def upgrade_folder_if_possible(self):
        """After the user grants access, move from the private folder to the normal one."""
        if self.args.dir == os.path.realpath(fallback_dir()) and has_all_files() \
                and can_use(preferred_dir()):
            core.set_share_dir(self.args, preferred_dir())
            self.dir_lbl.text = self.args.dir

    # -------------------------------------------------------------- tick ---
    def tick(self, _dt):
        S = core.Stats
        self.speed_lbl.text = f"Sending {core.speed(S.down)}\nReceiving {core.speed(S.up)}"
        self.total_lbl.text = f"Total sent {core.human(S.sent)}   |   received {core.human(S.received)}"
        self.perm_lbl.text = "" if has_all_files() else \
            "Using the private app folder. Tap 'Change folder' after allowing All files access."

        snap = S.snapshot()
        lines = []
        for k in sorted(snap, reverse=True)[:6]:
            t = snap[k]
            total = t["total"]
            if t["finished"]:
                pct = "done"
            elif total:
                pct = f"{min(100, int(t['done'] * 100 / total))}%"
            else:
                pct = "..."
            lines.append(f"{'Sending' if t['dir'] == 'out' else 'Receiving'}: {t['name']}  {pct}")
        self.tr_lbl.text = "\n".join(lines) or "No transfers yet"

        self.refresh_peers()
        self.status_lbl.text = self.fatal or self.msg["t"]

        self.ticks += 1
        if self.ticks % 6 == 0:
            new_ip = get_ip()
            if new_ip and new_ip != self.ip and not new_ip.startswith("127."):
                self.ip = new_ip
                self.update_address()
                self.warn.text = (f"Your address changed to {new_ip}. "
                                  "Scan the QR code again on the other device.")

    def refresh_peers(self):
        peers = core.Peers.alive()
        ids = sorted(peers, key=lambda k: peers[k]["name"].lower())
        labels = [peers[k]["name"] + "  (" + peers[k]["ip"] + ")"
                  + ("  [needs password]" if peers[k]["auth"] else "") for k in ids]
        key = (tuple(ids), tuple(labels), self.sel)
        if key == self._peer_key:
            return
        self._peer_key = key
        self.peer_box.clear_widgets()
        if not ids:
            self.peer_box.add_widget(wlabel("Looking for devices... both must be on the same "
                                            "Wi-Fi with Unisend open.", MUT, 13))
        for pid, lab in zip(ids, labels):
            b = Button(text=lab, size_hint_y=None, height=dp(48), background_normal="",
                       background_color=BTN_ON if pid == self.sel else BTN, color=FG,
                       font_size=sp(14))
            b.bind(on_release=lambda _b, pid=pid: self.select_peer(pid))
            self.peer_box.add_widget(b)

    def select_peer(self, pid):
        self.sel = pid

    # ------------------------------------------------------------ actions ---
    def chosen_peer(self):
        peer = core.Peers.alive().get(self.sel) if self.sel else None
        if not peer:
            self.msg["t"] = "Tap a device in the list first."
        return peer

    def on_browse(self):
        peer = self.chosen_peer()
        if peer:
            open_url(f"http://{peer['ip']}:{peer['port']}")

    def on_send(self):
        peer = self.chosen_peer()
        if not peer:
            return
        if peer["auth"]:
            self.ask_login(peer, lambda u, p: self.pick_files(peer, u, p))
        else:
            self.pick_files(peer, None, None)

    def ask_login(self, peer, done):
        body = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        body.add_widget(wlabel(f"{peer['name']} needs a login.", FG, 14))
        user = TextInput(hint_text="Username", multiline=False, size_hint_y=None, height=dp(46))
        pw = TextInput(hint_text="Password", password=True, multiline=False,
                       size_hint_y=None, height=dp(46))
        body.add_widget(user)
        body.add_widget(pw)
        pop = Popup(title="Login", content=body, size_hint=(0.92, None), height=dp(360))

        def go(*_):
            pop.dismiss()
            done(user.text, pw.text)
        body.add_widget(button("Continue", go, BTN_ON))
        body.add_widget(button("Cancel", lambda *_: pop.dismiss()))
        pop.open()

    def browse_root(self):
        for p in ("/storage/emulated/0", self.args.dir, os.path.expanduser("~")):
            if os.path.isdir(p):
                return p
        return "/"

    def pick_files(self, peer, user, password):
        body = BoxLayout(orientation="vertical", spacing=dp(8))
        fc = FileChooserListView(path=self.browse_root(), multiselect=True)
        body.add_widget(fc)
        pop = Popup(title=f"Choose files to send to {peer['name']}", content=body,
                    size_hint=(0.97, 0.92))

        def go(*_):
            paths = [p for p in fc.selection if os.path.isfile(p)]
            pop.dismiss()
            if paths:
                self.send_worker(peer, paths, user, password)
            else:
                self.msg["t"] = "No files were chosen."
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        row.add_widget(button("Send", go, BTN_ON))
        row.add_widget(button("Cancel", lambda *_: pop.dismiss()))
        body.add_widget(row)
        pop.open()

    def send_worker(self, peer, paths, user, password):
        def worker():
            ok_n, err = 0, ""
            for i, pth in enumerate(paths, 1):
                self.msg["t"] = (f"Sending {os.path.basename(pth)} to {peer['name']} "
                                 f"({i} of {len(paths)})...")
                ok, why = core.send_to_peer(peer, pth, self.args.name, user, password)
                if ok:
                    ok_n += 1
                else:
                    err = why
                    break
            self.msg["t"] = (f"Stopped: {err}. Sent {ok_n} of {len(paths)}." if err
                             else f"Sent {ok_n} file(s) to {peer['name']}.")
        threading.Thread(target=worker, daemon=True).start()

    def pick_folder(self):
        body = BoxLayout(orientation="vertical", spacing=dp(8))
        fc = FileChooserListView(path=self.args.dir if os.path.isdir(self.args.dir)
                                 else self.browse_root(), dirselect=True,
                                 filters=[lambda folder, name: os.path.isdir(os.path.join(folder, name))])
        body.add_widget(fc)
        pop = Popup(title="Choose the folder to share", content=body, size_hint=(0.97, 0.92))

        def go(*_):
            chosen = fc.selection[0] if fc.selection else fc.path
            pop.dismiss()
            if can_use(chosen):
                core.set_share_dir(self.args, chosen)
                self.dir_lbl.text = self.args.dir
            else:
                self.msg["t"] = "Unisend is not allowed to use that folder."
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        row.add_widget(button("Use this folder", go, BTN_ON))
        row.add_widget(button("Cancel", lambda *_: pop.dismiss()))
        body.add_widget(row)
        pop.open()


if __name__ == "__main__":
    UnisendApp().run()
