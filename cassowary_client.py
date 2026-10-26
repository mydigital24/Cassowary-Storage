#!/usr/bin/env python3
"""Cassowary - Schlichter WebDAV-Client (GTK4/libadwaita, nur Standardbibliothek).

Start: python3 webdav_client.py   (Pakete: python3-gobject gtk4 libadwaita)
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

CONFIG = Path(GLib.get_user_config_dir()) / "cassowary" / "config.json"
NS = {"d": "DAV:"}
BODY = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
        "<d:getcontentlength/></d:prop></d:propfind>")
CHUNK = 64 * 1024


def load_config():
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        cfg = {}
    if "accounts" not in cfg:
        url = cfg.get("url", "")
        user = cfg.get("user", "")
        password = cfg.get("password", "")
        cfg["accounts"] = [{"name": "Standard", "url": url, "user": user, "password": password}]
        cfg.setdefault("active_account", 0)
    if "automations" not in cfg:
        auto = cfg.get("auto", {})
        if auto:
            cfg["automations"] = [{
                "name": "Standard Auto",
                "enabled": auto.get("enabled", False),
                "direction": "local_to_server",
                "local_folder": auto.get("folder", ""),
                "remote_target": auto.get("target", "/"),
                "minutes": auto.get("minutes", 15),
                "account_idx": 0
            }]
        else:
            cfg["automations"] = []
    return cfg


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    CONFIG.chmod(0o600)


STATE = CONFIG.parent / "auto_state.json"


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def iter_files(folder):
    """Alle normalen Dateien (ohne Versteckte/Temporäre): (relativ, voll, mtime, größe)."""
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.startswith(".") or f.endswith((".part", ".tmp", ".crdownload")):
                continue
            full = os.path.join(root, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield os.path.relpath(full, folder).replace(os.sep, "/"), full, int(st.st_mtime), st.st_size


class Reader:
    """Datei-Wrapper, der beim Hochladen den Fortschritt meldet."""

    def __init__(self, f, cb, check_stop=None):
        self.f, self.cb, self.check_stop = f, cb, check_stop

    def read(self, n=-1):
        if self.check_stop and self.check_stop():
            raise InterruptedError("Abgebrochen")
        b = self.f.read(n)
        if b:
            self.cb(len(b))
        return b


class DavClient:
    def __init__(self, url, user, password):
        self.base = url.rstrip("/")
        self.root = urllib.parse.unquote(urllib.parse.urlparse(self.base).path).rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def url(self, path):
        return self.base + urllib.parse.quote(path)

    def _open(self, method, path, data=None, headers=None):
        req = urllib.request.Request(self.url(path), data=data, method=method)
        req.add_header("Authorization", self.auth)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return urllib.request.urlopen(req, timeout=30)

    def list(self, path):
        with self._open("PROPFIND", path, BODY.encode(),
                        {"Depth": "1", "Content-Type": "application/xml"}) as resp:
            tree = ET.fromstring(resp.read())
        items = []
        for r in tree.findall("d:response", NS):
            href = urllib.parse.unquote(urllib.parse.urlparse(r.findtext("d:href", "", NS)).path)
            rel = href[len(self.root):] if href.startswith(self.root) else href
            if rel.rstrip("/") == path.rstrip("/"):
                continue
            name = rel.rstrip("/").split("/")[-1]
            if name in ("", ".", ".."):
                continue
            is_dir = r.find(".//d:resourcetype/d:collection", NS) is not None
            size = r.findtext(".//d:getcontentlength", None, NS)
            items.append((name, is_dir, int(size) if size else None))
        items.sort(key=lambda i: (not i[1], i[0].casefold()))
        return items

    def walk(self, path, rel=""):
        for name, is_dir, size in self.list(path):
            if is_dir:
                yield rel + name + "/", True, 0
                yield from self.walk(path + name + "/", rel + name + "/")
            else:
                yield rel + name, False, size or 0

    def download(self, path, target, cb):
        with self._open("GET", path) as resp, open(target, "wb") as f:
            while chunk := resp.read(CHUNK):
                f.write(chunk)
                cb(len(chunk))

    def upload(self, path, local, cb):
        with open(local, "rb") as f:
            self._open("PUT", path, Reader(f, cb),
                       {"Content-Length": str(os.path.getsize(local))}).close()

    def mkdir(self, path):
        try:
            self._open("MKCOL", path).close()
        except urllib.error.HTTPError as e:
            if e.code != 405:  # 405 = existiert schon
                raise

    def delete(self, path):
        self._open("DELETE", path).close()

    def move(self, src, dst):
        self._open("MOVE", src, headers={"Destination": self.url(dst), "Overwrite": "F"}).close()


class ServerFolderPickerDialog(Adw.Dialog):
    def __init__(self, parent_win, client, initial_path, callback):
        super().__init__(title="Server-Ordner auswählen", content_width=450, content_height=500)
        self.client = client
        self.current_path = initial_path if initial_path.startswith("/") else "/"
        self.callback = callback
        self.parent_win = parent_win

        self.win_title = Adw.WindowTitle(title="Server-Ordner wählen", subtitle=self.current_path)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(self.win_title)
        
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda *_: self.close())
        select_btn = Gtk.Button(label="Auswählen")
        select_btn.add_css_class("suggested-action")
        select_btn.connect("clicked", lambda *_: (self.callback(self.current_path), self.close()))
        
        header.pack_start(cancel)
        header.pack_end(select_btn)

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.list_box.add_css_class("boxed-list")
        
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.list_box)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroll)
        self.set_child(view)

        self.load_contents()

    def load_contents(self):
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)

        if self.current_path != "/":
            row = Adw.ActionRow(title="..", subtitle="Übergeordneter Ordner")
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            row.set_activatable(True)
            parent_p = "/".join(self.current_path.rstrip("/").split("/")[:-1]) or "/"
            if not parent_p.endswith("/"):
                parent_p += "/"
            row.connect("activated", lambda *_: (setattr(self, "current_path", parent_p), self.load_contents()))
            self.list_box.append(row)

        def work():
            try:
                items = self.client.list(self.current_path)
            except Exception:
                items = []
            
            def fill():
                for name, is_dir, size in items:
                    if not is_dir:
                        continue
                    r = Adw.ActionRow(title=name, subtitle="Ordner")
                    r.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                    r.set_activatable(True)
                    sub_p = f"{self.current_path}{name}/"
                    r.connect("activated", lambda _, p=sub_p: (setattr(self, "current_path", p), self.load_contents()))
                    self.list_box.append(r)
                self.win_title.set_subtitle(self.current_path)
            GLib.idle_add(fill)

        threading.Thread(target=work, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Cassowary", default_width=600, default_height=700)
        self.cfg = load_config()
        self.client = None
        self.path = "/"
        self.busy = False
        self.stop_requested = False
        self.done = self.total = 0
        self._t = 0.0
        self.auto_ids = []
        self.state_data = load_state()

        header = Adw.HeaderBar()
        self.back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Zurück (Drop: nach oben verschieben)")
        self.back.connect("clicked", self.go_up)
        up_drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        up_drop.connect("drop", lambda t, v, x, y: self.drop_move(v, self.parent_path()))
        self.back.add_controller(up_drop)
        header.pack_start(self.back)
        self.title = Adw.WindowTitle(title="Cassowary", subtitle="/")
        header.set_title_widget(self.title)

        # Server status at the top
        self.server_status_lbl = Gtk.Label(label="Ready", css_classes=["dim-label"])
        header.pack_start(self.server_status_lbl)

        # CalDAV accounts dropdown
        accounts = self.cfg.get("accounts", [])
        acc_names = [a.get("name", f"Account {i+1}") for i, a in enumerate(accounts)]
        self.account_strings = Gtk.StringList.new(acc_names if acc_names else ["Standard"])
        self.account_dropdown = Gtk.DropDown.new(self.account_strings, None)
        self.account_dropdown.set_selected(self.cfg.get("active_account", 0))
        self.account_dropdown.connect("notify::selected", self.on_account_changed)
        header.pack_start(self.account_dropdown)

        menu = Gio.Menu()
        menu.append("Dateien hochladen", "win.upload")
        menu.append("Ordner hochladen", "win.upload_dir")
        menu.append("Neuer Ordner", "win.mkdir")
        header.pack_end(Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=menu, tooltip_text="Hinzufügen"))
        sett = Gtk.Button(icon_name="preferences-system-symbolic", tooltip_text="Einstellungen")
        sett.connect("clicked", lambda *_: self.show_settings())
        header.pack_end(sett)
        for name, cb in (("upload", self.on_upload), ("upload_dir", self.on_upload_dir), ("mkdir", self.on_mkdir)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        item_act = Gio.SimpleAction.new("item", GLib.VariantType.new("s"))
        item_act.connect("activate", self.on_item_action)
        self.add_action(item_act)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True,
                                valign=Gtk.Align.START, min_children_per_line=2, max_children_per_line=30,
                                activate_on_single_click=False, row_spacing=6, column_spacing=6,
                                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.flow.connect("child-activated", self.on_child_activated)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.flow)
        self.status = Adw.StatusPage(icon_name="network-server-symbolic", title="Nicht verbunden")
        self.stack = Gtk.Stack()
        self.stack.add_named(scroll, "list")
        self.stack.add_named(self.status, "status")

        # Dateien aus dem Dateimanager hineinziehen = hochladen
        files_drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        files_drop.connect("drop", self.on_files_dropped)
        self.stack.add_controller(files_drop)

        self.bar = Gtk.ProgressBar()
        self.bar_label = Gtk.Label(xalign=0, ellipsize=3)
        self.stop_btn = Gtk.Button(label="Stoppen", css_classes=["destructive-action"])
        self.stop_btn.connect("clicked", lambda *_: setattr(self, "stop_requested", True))

        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=True, hexpand=True)
        info_box.append(self.bar_label)
        info_box.append(self.bar)
        progress_box.append(info_box)
        progress_box.append(self.stop_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=8,
                      margin_start=12, margin_end=12)
        box.append(progress_box)
        self.rev = Gtk.Revealer(child=box)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_bottom_bar(self.rev)
        view.set_content(self.toasts)
        self.set_content(view)

        if self.get_active_account().get("url"):
            self.connect_client()
        else:
            self.stack.set_visible_child_name("status")
            self.set_server_status("Error", is_error=True)
            GLib.idle_add(lambda: self.show_settings() and False)
        self.update_header()
        self.schedule_auto()

    # --- Hilfen ----------------------------------------------------------
    def get_active_account(self):
        accounts = self.cfg.get("accounts", [])
        idx = self.cfg.get("active_account", 0)
        if 0 <= idx < len(accounts):
            return accounts[idx]
        if accounts:
            return accounts[0]
        return {"name": "Standard", "url": "", "user": "", "password": ""}

    def on_account_changed(self, dropdown, _pspec):
        idx = dropdown.get_selected()
        if idx != self.cfg.get("active_account", 0):
            self.cfg["active_account"] = idx
            save_config(self.cfg)
            self.connect_client()

    def set_server_status(self, text, is_error=False):
        GLib.idle_add(lambda: (
            self.server_status_lbl.set_label(text),
            self.server_status_lbl.set_css_classes(["error" if is_error else "dim-label"]),
            False
        )[2])

    def check_stop(self):
        return self.stop_requested

    def say(self, msg):
        GLib.idle_add(lambda: (self.toasts.add_toast(Adw.Toast.new(msg)), False)[1])

    def update_header(self):
        self.back.set_sensitive(self.path != "/")
        self.title.set_subtitle(self.path)

    def error_text(self, e):
        if isinstance(e, urllib.error.HTTPError):
            return "Anmeldung fehlgeschlagen" if e.code == 401 else f"Server-Fehler {e.code}"
        return str(getattr(e, "reason", e))

    def parent_path(self):
        return self.path.rstrip("/").rsplit("/", 1)[0] + "/"

    def connect_client(self):
        acc = self.get_active_account()
        url, user, password = acc.get("url", ""), acc.get("user", ""), acc.get("password", "")
        if url:
            try:
                self.client = DavClient(url, user, password)
                self.path = "/"
                self.set_server_status("Ready")
                self.refresh()
            except Exception as e:
                self.client = None
                self.set_server_status("Error", is_error=True)
                self.show_error(self.error_text(e))
        else:
            self.client = None
            self.set_server_status("Error", is_error=True)

    def bg(self, fn):
        """Kurze Aktion im Hintergrund, danach Liste neu laden."""
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.say(self.error_text(e))
            GLib.idle_add(lambda: (self.refresh(), False)[1])
        threading.Thread(target=run, daemon=True).start()

    # --- Übertragungen mit Fortschritt ---------------------------------------
    def add(self, n):
        self.done += n
        now = time.monotonic()
        if now - self._t > 0.1 or self.done >= self.total:
            self._t = now
            GLib.idle_add(self.bar.set_fraction, min(1.0, self.done / max(self.total, 1)))

    def job(self, label, work):
        if self.busy:
            self.say("Es läuft bereits eine Übertragung")
            return
        self.stop_requested = False
        self.busy, self.done, self.total = True, 0, 1
        self.bar_label.set_text(label)
        self.bar.set_fraction(0)
        self.rev.set_reveal_child(True)

        def run():
            try:
                work()
            except InterruptedError:
                self.say("Übertragung abgebrochen")
            except Exception as e:  # noqa: BLE001
                self.say(self.error_text(e))
            GLib.idle_add(self.finish)
        threading.Thread(target=run, daemon=True).start()

    def finish(self):
        self.busy = False
        self.rev.set_reveal_child(False)
        self.refresh()
        return False

    def label(self, text):
        GLib.idle_add(self.bar_label.set_text, text)

    # --- Liste -----------------------------------------------------------
    def refresh(self):
        if not self.client:
            return
        self.update_header()
        path = self.path

        def work():
            try:
                items = self.client.list(path)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.show_error, self.error_text(e))
                return
            GLib.idle_add(self.fill, path, items)
        threading.Thread(target=work, daemon=True).start()

    def show_error(self, text):
        self.status.set_title("Fehler")
        self.status.set_description(text)
        self.stack.set_visible_child_name("status")

    def fill(self, path, items):
        if path != self.path:
            return
        while (c := self.flow.get_first_child()) is not None:
            self.flow.remove(c)
        if not items:
            self.status.set_title("Leer")
            self.status.set_description("Dateien hierher ziehen oder mit „+“ hinzufügen.")
            self.stack.set_visible_child_name("status")
            return
        for name, is_dir, size in items:
            self.flow.append(self.make_item(name, is_dir, size))
        self.stack.set_visible_child_name("list")

    def make_item(self, name, is_dir, size):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=8, margin_bottom=8, margin_start=4, margin_end=4)
        box.info = (name, is_dir, size)
        if is_dir:
            img = Gtk.Image.new_from_icon_name("folder")
        else:
            img = Gtk.Image.new_from_gicon(Gio.content_type_get_icon(Gio.content_type_guess(name, None)[0]))
        img.set_pixel_size(64)
        lbl = Gtk.Label(label=name, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                        ellipsize=Pango.EllipsizeMode.END, justify=Gtk.Justification.CENTER,
                        max_width_chars=14, width_chars=14)
        box.append(img)
        box.append(lbl)
        box.set_tooltip_text(name if size is None else f"{name}\n{GLib.format_size(size)}")

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda g, n, x, y: self.context_menu(box, x, y))
        box.add_controller(click)

        if is_dir:
            drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
            drop.connect("drop", lambda t, v, x, y: self.drop_move(v, f"{self.path}{name}/"))
            box.add_controller(drop)
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda s, x, y: Gdk.ContentProvider.new_for_value(
            GObject.Value(str, f"{name}\t{int(is_dir)}")))
        box.add_controller(src)
        return box

    def on_child_activated(self, _flow, child):
        name, is_dir, size = child.get_child().info
        if is_dir:
            self.open_dir(name)
        else:
            self.open_item(name, size)

    def context_menu(self, widget, x, y):
        name, is_dir, size = widget.info
        self.flow.select_child(widget.get_parent())
        menu = Gio.Menu()
        ops = ([("open", "Öffnen")] + ([] if is_dir else [("openwith", "Öffnen mit …")]) +
               [("download", "Herunterladen"), ("rename", "Umbenennen …"),
                ("move", "Verschieben …"), ("delete", "Löschen …")])
        for op, text in ops:
            it = Gio.MenuItem.new(text, None)
            it.set_action_and_target_value(
                "win.item", GLib.Variant("s", f"{op}\t{name}\t{int(is_dir)}\t{size or ''}"))
            menu.append_item(it)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_parent(widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def on_item_action(self, _action, param):
        op, name, flag, size = param.get_string().split("\t")
        is_dir, size = flag == "1", (int(size) if size else None)
        if op == "open":
            if is_dir:
                self.open_dir(name)
            else:
                self.open_item(name, size)
        elif op == "openwith":
            self.open_item(name, size, True)
        elif op == "download":
            self.download_item(name, is_dir, size)
        elif op == "rename":
            self.rename_item(name, is_dir)
        elif op == "move":
            self.move_dialog(name, is_dir)
        elif op == "delete":
            self.delete_item(name, is_dir)

    def open_dir(self, name):
        self.path = f"{self.path}{name}/"
        self.refresh()

    def go_up(self, *_):
        self.path = self.parent_path()
        self.refresh()

    # --- Aktionen: Download / Löschen / Umbenennen / Verschieben ---------------
    def download_item(self, name, is_dir, size):
        dl = self.download_dir()
        base = self.path

        def work():
            if is_dir:
                items = list(self.client.walk(f"{base}{name}/"))
                self.total = sum(s for _, d, s in items if not d) or 1
                root = os.path.join(dl, name)
                os.makedirs(root, exist_ok=True)
                for rel, d, _s in items:
                    if d:
                        os.makedirs(os.path.join(root, rel), exist_ok=True)
                    else:
                        self.label(f"Herunterladen: {rel}")
                        self.client.download(f"{base}{name}/{rel}", os.path.join(root, rel), self.add)
            else:
                self.total = size or 1
                self.client.download(base + name, os.path.join(dl, name), self.add)
            self.say(f"Gespeichert in {dl}")
        self.job(f"Herunterladen: {name}", work)

    def ask(self, heading, body, ok_label, cb, entry_text=None, destructive=False):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        entry = None
        if entry_text is not None:
            entry = Gtk.Entry(text=entry_text, activates_default=True)
            dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", ok_label)
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                    else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def done(d, res):
            if d.choose_finish(res) == "ok":
                cb(entry.get_text().strip() if entry else None)
        dlg.choose(self, None, done)

    def delete_item(self, name, is_dir):
        what = "den Ordner samt Inhalt" if is_dir else "die Datei"
        path = self.path + name + ("/" if is_dir else "")
        self.ask("Löschen?", f"Soll {what} „{name}“ endgültig gelöscht werden?", "Löschen",
                 lambda _t: self.bg(lambda: self.client.delete(path)), destructive=True)

    def rename_item(self, name, is_dir):
        sfx = "/" if is_dir else ""

        def go(new):
            if new and new != name and "/" not in new:
                self.bg(lambda: self.client.move(self.path + name + sfx, self.path + new + sfx))
        self.ask("Umbenennen", "", "Umbenennen", go, entry_text=name)

    def move_dialog(self, name, is_dir):
        def go(target):
            if target is not None:
                self.move_to(name, is_dir, "/" + target.strip("/") + "/" if target.strip("/") else "/")
        self.ask("Verschieben", "Zielordner (Pfad ab Server-Wurzel):", "Verschieben", go, entry_text=self.path)

    def move_to(self, name, is_dir, target_dir, source_dir=None):
        src_dir = source_dir or self.path
        sfx = "/" if is_dir else ""
        if src_dir == target_dir or (is_dir and target_dir.startswith(f"{src_dir}{name}/")):
            return
        self.bg(lambda: self.client.move(f"{src_dir}{name}{sfx}", f"{target_dir}{name}{sfx}"))

    def drop_move(self, value, target_dir):
        try:
            name, flag = value.rsplit("\t", 1)
        except ValueError:
            return False
        self.move_to(name, flag == "1", target_dir)
        return True

    # --- Hinzufügen / Upload ---------------------------------------------------
    def upload_paths(self, paths, dest):
        files, dirs = [], []
        for p in paths:
            base = os.path.basename(p.rstrip("/"))
            if os.path.isdir(p):
                dirs.append(f"{dest}{base}/")
                for root, ds, fs in os.walk(p):
                    rel = os.path.relpath(root, p)
                    rp = f"{dest}{base}/" + ("" if rel == "." else rel + "/")
                    dirs += [rp + d + "/" for d in ds]
                    files += [(os.path.join(root, f), rp + f) for f in fs]
            else:
                files.append((p, dest + base))

        def work():
            self.total = sum(os.path.getsize(l) for l, _ in files) or 1
            for d in dirs:
                self.client.mkdir(d)
            for local, remote in files:
                self.label(f"Hochladen: {os.path.basename(local)}")
                self.client.upload(remote, local, self.add)
            self.say("Upload fertig")
        self.job("Hochladen …", work)

    def on_files_dropped(self, target, value, x, y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if paths and self.client:
            self.upload_paths(paths, self.path)
        return True

    def on_upload(self, *_):
        if not self.client:
            return

        def done(dlg, res):
            try:
                model = dlg.open_multiple_finish(res)
            except GLib.Error:
                return
            self.upload_paths([model.get_item(i).get_path() for i in range(model.get_n_items())], self.path)
        Gtk.FileDialog(title="Dateien hochladen").open_multiple(self, None, done)

    def on_upload_dir(self, *_):
        if not self.client:
            return

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            self.upload_paths([f.get_path()], self.path)
        Gtk.FileDialog(title="Ordner hochladen").select_folder(self, None, done)

    def on_mkdir(self, *_):
        if not self.client:
            return

        def go(name):
            if name:
                self.bg(lambda: self.client.mkdir(f"{self.path}{name}/"))
        self.ask("Neuer Ordner", "", "Erstellen", go, entry_text="")

    # --- Öffnen, Backup, Einstellungen ---------------------------------------
    def download_dir(self):
        d = self.cfg.get("download_dir")
        if not d:
            try:
                d = GLib.get_user_special_directory(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            except Exception:  # noqa: BLE001
                d = None
        d = d or str(Path.home() / "Downloads")
        os.makedirs(d, exist_ok=True)
        return d

    def open_item(self, name, size, always_ask=False):
        """Datei in den Cache laden und mit Standard-App (oder „Öffnen mit …“) starten."""
        tmp = os.path.join(GLib.get_user_cache_dir(), "cassowary", "open")
        os.makedirs(tmp, exist_ok=True)
        target, remote = os.path.join(tmp, name), self.path + name

        def work():
            self.total = size or 1
            self.client.download(remote, target, self.add)
            GLib.idle_add(self.launch, target, always_ask)
        self.job(f"Öffnen: {name}", work)

    def launch(self, target, always_ask):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(target))
        launcher.set_always_ask(always_ask)

        def done(l, res):
            try:
                l.launch_finish(res)
            except GLib.Error:
                pass  # z. B. Dialog abgebrochen
        launcher.launch(self, None, done)
        return False

    def backup_zip(self, root, target):
        def work():
            items = list(self.client.walk(root))
            self.total = sum(s for _, d, s in items if not d) or 1
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for rel, is_dir, _s in items:
                    if is_dir:
                        z.writestr(rel, "")
                        continue
                    self.label(f"Sichern: {rel}")
                    with z.open(rel, "w", force_zip64=True) as out, self.client._open("GET", root + rel) as resp:
                        while chunk := resp.read(CHUNK):
                            out.write(chunk)
                            self.add(len(chunk))
            self.say(f"Backup gespeichert: {target}")
        self.job("Backup wird erstellt …", work)

    # --- Mehrere Automationen (Lokal <-> Server / Cloud <-> Cloud) ----------------------------
    def schedule_auto(self):
        for sid in self.auto_ids:
            GLib.source_remove(sid)
        self.auto_ids = []
        automations = self.cfg.get("automations", [])
        for a in automations:
            if a.get("enabled"):
                direction = a.get("direction", "local_to_server")
                if direction == "cloud_to_cloud" or a.get("folder") or a.get("local_folder"):
                    minutes = max(1, int(a.get("minutes", 15)))
                    sid = GLib.timeout_add_seconds(minutes * 60, lambda auto=a: (self.run_automation(auto), True)[1])
                    self.auto_ids.append(sid)

    def run_automation(self, a, manual=False):
        if self.busy:
            if manual:
                self.say("Es läuft bereits eine Übertragung")
            return

        folder = a.get("folder") or a.get("local_folder")
        target = a.get("target", "/")
        direction = a.get("direction", "local_to_server")
        acc_idx = a.get("account_idx", 0)
        auto_name = a.get("name", "Auto")

        accounts = self.cfg.get("accounts", [])
        if direction == "cloud_to_cloud":
            src_acc_idx = a.get("source_account_idx", 0)
            src_acc = accounts[src_acc_idx] if 0 <= src_acc_idx < len(accounts) else (accounts[0] if accounts else None)
            tgt_acc = accounts[acc_idx] if 0 <= acc_idx < len(accounts) else (accounts[0] if accounts else None)
            if not src_acc or not tgt_acc:
                if manual:
                    self.say("Quell- oder Zielkonto nicht konfiguriert")
                return

            src_path = a.get("source_path", "/")
            src_client = DavClient(src_acc["url"], src_acc["user"], src_acc["password"])
            tgt_client = DavClient(tgt_acc["url"], tgt_acc["user"], tgt_acc["password"])
            autostate = self.state_data.setdefault("automations", {}).setdefault(auto_name, {})
            seen = autostate.setdefault("seen", {})

            def work():
                if self.check_stop():
                    raise InterruptedError()
                self.label(f"Cloud-Sync ({auto_name}): Lese Quell-Cloud...")
                try:
                    remote_items = list(src_client.walk(src_path))
                except Exception as e:
                    if manual:
                        self.say(self.error_text(e))
                    return

                sync_list = []
                for rel, is_dir, size in remote_items:
                    if is_dir:
                        continue
                    remote_sig = [size]
                    if seen.get(rel) != remote_sig:
                        sync_list.append((rel, size))

                if not sync_list:
                    if manual:
                        self.say("Nichts Neues in der Quell-Cloud")
                    return

                self.total = sum(sz for _, sz in sync_list) or 1
                made = set()
                self.ensure_client_dirs(tgt_client, target, made)

                for rel, size in sync_list:
                    if self.check_stop():
                        raise InterruptedError()
                    parent = rel.rpartition("/")[0]
                    if parent:
                        self.ensure_client_dirs(tgt_client, f"{target.rstrip('/')}/{parent}/", made)

                    self.label(f"Cloud-Sync ({auto_name}): {rel}")
                    tmp_fd, tmp_path = tempfile.mkstemp()
                    os.close(tmp_fd)
                    try:
                        src_client.download(src_path.rstrip("/") + "/" + rel, tmp_path, self.add)
                        with open(tmp_path, "rb") as f:
                            tgt_client.upload(target.rstrip("/") + "/" + rel, tmp_path, lambda n: None)
                        seen[rel] = [size]
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

                save_state(self.state_data)
                self.say(f"Cloud-Sync ({auto_name}): {len(sync_list)} Datei(en) synchronisiert")
            self.job(f"Cloud-Sync: {auto_name}", work)
            return

        if 0 <= acc_idx < len(accounts):
            acc = accounts[acc_idx]
        elif accounts:
            acc = accounts[0]
        else:
            if manual:
                self.say("Kein Konto konfiguriert")
            return

        if not folder or not os.path.isdir(folder):
            if manual:
                self.say("Lokaler Ordner nicht gewählt oder ungültig")
            return

        client = DavClient(acc["url"], acc["user"], acc["password"])
        autostate = self.state_data.setdefault("automations", {}).setdefault(auto_name, {})
        seen = autostate.setdefault("seen", {})

        if direction == "local_to_server":
            def work():
                if self.check_stop():
                    raise InterruptedError()
                now = time.time()
                new = [(full, rel, mt, sz) for rel, full, mt, sz in iter_files(folder)
                       if now - mt > 5 and seen.get(rel) != [mt, sz]]
                if not new:
                    if manual:
                        self.say("Nichts Neues im Ordner")
                    return
                self.total = sum(sz for _, _, _, sz in new) or 1
                made = set()
                self.ensure_client_dirs(client, target, made)
                for local, rel, mt, sz in new:
                    if self.check_stop():
                        raise InterruptedError()
                    parent = rel.rpartition("/")[0]
                    if parent:
                        self.ensure_client_dirs(client, f"{target}{parent}/", made)
                    self.label(f"Auto-Upload ({auto_name}): {rel}")
                    with open(local, "rb") as f:
                        client.upload(target + rel, local, self.add)
                    seen[rel] = [mt, sz]
                save_state(self.state_data)
                self.say(f"Auto-Upload ({auto_name}): {len(new)} Datei(en) hochgeladen")
            self.job(f"Auto-Upload: {auto_name}", work)

        elif direction == "server_to_local":
            def work():
                if self.check_stop():
                    raise InterruptedError()
                self.label(f"Auto-Download ({auto_name}): Prüfe Server...")
                try:
                    remote_items = list(client.walk(target))
                except Exception as e:
                    if manual:
                        self.say(self.error_text(e))
                    return
                os.makedirs(folder, exist_ok=True)
                download_list = []
                for rel, is_dir, size in remote_items:
                    if is_dir:
                        os.makedirs(os.path.join(folder, rel), exist_ok=True)
                        continue
                    local_path = os.path.join(folder, rel)
                    remote_sig = [size]
                    if not os.path.exists(local_path) or seen.get(rel) != remote_sig:
                        download_list.append((rel, local_path, size))

                if not download_list:
                    if manual:
                        self.say("Nichts Neues auf dem Server")
                    return

                self.total = sum(sz for _, _, sz in download_list) or 1
                for rel, local_path, size in download_list:
                    if self.check_stop():
                        raise InterruptedError()
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    self.label(f"Auto-Download ({auto_name}): {rel}")
                    client.download(target.rstrip("/") + "/" + rel, local_path, self.add)
                    seen[rel] = [size]
                save_state(self.state_data)
                self.say(f"Auto-Download ({auto_name}): {len(download_list)} Datei(en) heruntergeladen")
            self.job(f"Auto-Download: {auto_name}", work)

    def ensure_client_dirs(self, client, path, made):
        cur = "/"
        for seg in [x for x in path.split("/") if x]:
            cur += seg + "/"
            if cur not in made:
                client.mkdir(cur)
                made.add(cur)

    # --- Einstellungen -------------------------------------------------------
    def show_settings(self):
        c = self.cfg
        accounts = c.get("accounts", [])

        conn = Adw.PreferencesGroup(title="Konto &amp; Verbindung", description="Verwalten Sie Ihre WebDAV/CalDAV Konten.")
        acc_mgr_row = Adw.ActionRow(title="Konten verwalten", subtitle=f"{len(accounts)} Konto (Konten) konfiguriert", activatable=True)
        acc_mgr_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        conn.add(acc_mgr_row)

        files = Adw.PreferencesGroup(title="Dateien")
        dl_row = Adw.ActionRow(title="Download-Ordner", subtitle=self.download_dir(), activatable=True)
        dl_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        files.add(dl_row)

        auto_group = Adw.PreferencesGroup(
            title="Automationen",
            description="Verwalten Sie automatische Synchronisationen unter Einstellungen -> Automationen.")
        auto_mgr_row = Adw.ActionRow(title="Automationen verwalten", subtitle=f"{len(c.get('automations', []))} Automation(en) konfiguriert", activatable=True)
        auto_mgr_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        auto_group.add(auto_mgr_row)

        config_group = Adw.PreferencesGroup(title="Konfiguration", description="Exportieren oder importieren Sie Ihre Einstellungen und Automationen.")
        export_row = Adw.ActionRow(title="Konfiguration exportieren", subtitle="Als JSON-Datei speichern", activatable=True)
        export_row.add_suffix(Gtk.Image(icon_name="document-save-symbolic"))
        import_row = Adw.ActionRow(title="Konfiguration importieren", subtitle="Aus JSON-Datei laden", activatable=True)
        import_row.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
        config_group.add(export_row)
        config_group.add(import_row)

        backup = Adw.PreferencesGroup(title="Backup",
                                      description="Lädt die Dateien vom Server und speichert sie als ZIP.")
        all_row = Adw.ActionRow(title="Alles sichern", subtitle="Kompletter Server als ZIP", activatable=True)
        cur_row = Adw.ActionRow(title="Aktuellen Ordner sichern", subtitle=self.path, activatable=True)
        for r in (all_row, cur_row):
            r.add_suffix(Gtk.Image(icon_name="document-save-symbolic"))
            backup.add(r)

        page = Adw.PreferencesPage()
        for g in (conn, files, auto_group, config_group, backup):
            page.add(g)

        dlg = Adw.Dialog(title="Einstellungen", content_width=480, content_height=720)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Einstellungen"))
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda *_: dlg.close())
        save = Gtk.Button(label="Speichern")
        save.add_css_class("suggested-action")
        header.pack_start(cancel)
        header.pack_end(save)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        dlg.set_child(view)

        def pick_folder(row_title, on_ok):
            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                on_ok(f.get_path())
            Gtk.FileDialog(title=row_title).select_folder(self, None, done)

        def set_dl(path):
            self.cfg["download_dir"] = path
            save_config(self.cfg)
            dl_row.set_subtitle(path)
        dl_row.connect("activated", lambda *_: pick_folder("Download-Ordner", set_dl))

        acc_mgr_row.connect("activated", lambda *_: (dlg.close(), self.show_accounts_dialog()))
        auto_mgr_row.connect("activated", lambda *_: (dlg.close(), self.show_automations_dialog()))

        def export_cfg(_):
            def done(d, res):
                try:
                    f = d.save_finish(res)
                except GLib.Error:
                    return
                try:
                    Path(f.get_path()).write_text(json.dumps(self.cfg, indent=2))
                    self.say("Konfiguration exportiert")
                except Exception as e:
                    self.say(str(e))
            fd = Gtk.FileDialog(title="Konfiguration exportieren", initial_name="cassowary-config.json")
            dlg.close()
            fd.save(self, None, done)
        export_row.connect("activated", export_cfg)

        def import_cfg(_):
            def done(d, res):
                try:
                    f = d.open_finish(res)
                except GLib.Error:
                    return
                try:
                    data = json.loads(Path(f.get_path()).read_text())
                    self.cfg = data
                    save_config(self.cfg)
                    self.schedule_auto()
                    acc_names = [a.get("name", f"Account {i+1}") for i, a in enumerate(self.cfg.get("accounts", []))]
                    self.account_strings.splice(0, self.account_strings.get_n_items(), acc_names)
                    self.connect_client()
                    self.say("Konfiguration importiert")
                except Exception as e:
                    self.say(f"Fehler beim Import: {e}")
            fd = Gtk.FileDialog(title="Konfiguration importieren")
            dlg.close()
            fd.open(self, None, done)
        import_row.connect("activated", import_cfg)

        def backup_to(root):
            if not self.client:
                self.say("Nicht verbunden")
                return

            def done(d, res):
                try:
                    f = d.save_finish(res)
                except GLib.Error:
                    return
                self.backup_zip(root, f.get_path())
            fd = Gtk.FileDialog(title="Backup speichern",
                                initial_name=f"cassowary-backup-{time.strftime('%Y-%m-%d')}.zip")
            dlg.close()
            fd.save(self, None, done)
        all_row.connect("activated", lambda *_: backup_to("/"))
        cur_row.connect("activated", lambda *_: backup_to(self.path))

        def do_save(*_):
            save_config(self.cfg)
            dlg.close()
        save.connect("clicked", do_save)

        dlg.present(self)
        return False

    def show_accounts_dialog(self):
        accounts = self.cfg.get("accounts", [])
        active_idx = self.cfg.get("active_account", 0)
        dlg = Adw.Dialog(title="Konten verwalten", content_width=520, content_height=600)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Konten verwalten"))
        close_btn = Gtk.Button(label="Schließen")
        close_btn.connect("clicked", lambda *_: (dlg.close(), self.show_settings()))
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Konto hinzufügen")
        header.pack_start(close_btn)
        header.pack_end(add_btn)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(page_box)

        def rebuild_list():
            while (child := page_box.get_first_child()) is not None:
                page_box.remove(child)
            group = Adw.PreferencesGroup(title="Konfigurierte Konten", description="Wählen Sie das aktive Konto oder bearbeiten/erstellen Sie Konten.")
            page_box.append(group)
            for idx, acc in enumerate(accounts):
                is_active = (idx == active_idx)
                row = Adw.ActionRow(
                    title=f"{acc.get('name', f'Konto {idx+1}')}{' (Aktiv)' if is_active else ''}",
                    subtitle=acc.get('url', 'Keine URL')
                )

                if not is_active:
                    switch_btn = Gtk.Button(label="Aktivieren", valign=Gtk.Align.CENTER)
                    def on_activate_acc(_, a_idx=idx):
                        self.cfg["active_account"] = a_idx
                        save_config(self.cfg)
                        acc_names = [a.get("name", f"Account {i+1}") for i, a in enumerate(self.cfg["accounts"])]
                        self.account_strings.splice(0, self.account_strings.get_n_items(), acc_names)
                        self.account_dropdown.set_selected(a_idx)
                        self.connect_client()
                        dlg.close()
                        self.show_accounts_dialog()
                    switch_btn.connect("clicked", on_activate_acc)
                    row.add_suffix(switch_btn)

                edit_btn = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
                def on_edit(_, a_idx=idx):
                    dlg.close()
                    self.show_edit_account_dialog(a_idx)
                edit_btn.connect("clicked", on_edit)
                row.add_suffix(edit_btn)

                if len(accounts) > 1:
                    del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                    del_btn.add_css_class("destructive-action")
                    def on_del(_, a_idx=idx):
                        nonlocal active_idx
                        accounts.pop(a_idx)
                        if active_idx >= len(accounts):
                            active_idx = len(accounts) - 1
                        self.cfg["active_account"] = active_idx
                        self.cfg["accounts"] = accounts
                        save_config(self.cfg)
                        acc_names = [a.get("name", f"Account {i+1}") for i, a in enumerate(self.cfg["accounts"])]
                        self.account_strings.splice(0, self.account_strings.get_n_items(), acc_names)
                        self.account_dropdown.set_selected(active_idx)
                        self.connect_client()
                        rebuild_list()
                    del_btn.connect("clicked", on_del)
                    row.add_suffix(del_btn)

                group.add(row)

        rebuild_list()
        add_btn.connect("clicked", lambda *_: (dlg.close(), self.show_edit_account_dialog(None)))

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroll)
        dlg.set_child(view)
        dlg.present(self)

    def show_edit_account_dialog(self, account_idx):
        accounts = self.cfg.get("accounts", [])
        is_new = account_idx is None or account_idx >= len(accounts)
        acc = accounts[account_idx] if not is_new else {
            "name": f"Konto {len(accounts)+1}",
            "url": "",
            "user": "",
            "password": ""
        }

        dlg = Adw.Dialog(title="Konto bearbeiten", content_width=460, content_height=500)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Konto"))
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda *_: (dlg.close(), self.show_accounts_dialog()))
        save = Gtk.Button(label="Speichern")
        save.add_css_class("suggested-action")
        header.pack_start(cancel)
        header.pack_end(save)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Kontodetails")

        name_row = Adw.EntryRow(title="Konto-Name")
        name_row.set_text(acc.get("name", ""))
        group.add(name_row)

        url_row = Adw.EntryRow(title="WebDAV-URL (https://…)")
        url_row.set_text(acc.get("url", ""))
        group.add(url_row)

        user_row = Adw.EntryRow(title="Benutzer")
        user_row.set_text(acc.get("user", ""))
        group.add(user_row)

        pw_row = Adw.PasswordEntryRow(title="Passwort")
        pw_row.set_text(acc.get("password", ""))
        group.add(pw_row)

        page.add(group)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(page)
        dlg.set_child(view)

        def do_save(*_):
            acc["name"] = name_row.get_text().strip() or "Konto"
            acc["url"] = url_row.get_text().strip()
            acc["user"] = user_row.get_text()
            acc["password"] = pw_row.get_text()
            if is_new:
                accounts.append(acc)
                account_idx = len(accounts) - 1
            else:
                accounts[account_idx] = acc
            self.cfg["accounts"] = accounts
            save_config(self.cfg)

            acc_names = [a.get("name", f"Account {i+1}") for i, a in enumerate(self.cfg["accounts"])]
            self.account_strings.splice(0, self.account_strings.get_n_items(), acc_names)
            if account_idx == self.cfg.get("active_account", 0):
                self.connect_client()

            dlg.close()
            self.show_accounts_dialog()
        save.connect("clicked", do_save)

        dlg.present(self)

    def show_automations_dialog(self):
        automations = self.cfg.get("automations", [])
        dlg = Adw.Dialog(title="Automationen verwalten", content_width=650, content_height=750)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Automationen"))
        close_btn = Gtk.Button(label="Schließen")
        close_btn.connect("clicked", lambda *_: (dlg.close(), self.show_settings()))
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Automation hinzufügen")
        header.pack_start(close_btn)
        header.pack_end(add_btn)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(page_box)

        def rebuild_list():
            while (child := page_box.get_first_child()) is not None:
                page_box.remove(child)
            group = Adw.PreferencesGroup(title="Konfigurierte Automationen")
            page_box.append(group)
            for idx, auto in enumerate(automations):
                d = auto.get("direction", "local_to_server")
                if d == "local_to_server":
                    dir_text = "Lokal -> Server"
                    det = f"Ordner: {auto.get('folder', '')}"
                elif d == "server_to_local":
                    dir_text = "Server -> Lokal"
                    det = f"Ordner: {auto.get('folder', '')}"
                else:
                    dir_text = "Cloud -> Cloud"
                    det = f"Src: {auto.get('source_path', '')} -> Tgt: {auto.get('target', '')}"

                row = Adw.ActionRow(
                    title=auto.get("name", f"Auto {idx+1}"),
                    subtitle=f"Richtung: {dir_text} | {det} | Alle {auto.get('minutes', 15)} Min"
                )
                sw = Gtk.Switch(valign=Gtk.Align.CENTER)
                sw.set_active(bool(auto.get("enabled")))
                def on_sw(s, _, a_item=auto):
                    a_item["enabled"] = s.get_active()
                    save_config(self.cfg)
                    self.schedule_auto()
                sw.connect("notify::active", on_sw)
                row.add_suffix(sw)

                run_btn = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Jetzt ausführen")
                def on_run(_, a_item=auto):
                    dlg.close()
                    self.run_automation(a_item, manual=True)
                run_btn.connect("clicked", on_run)
                row.add_suffix(run_btn)

                edit_btn = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
                def on_edit(_, a_idx=idx):
                    dlg.close()
                    self.show_edit_automation_dialog(a_idx)
                edit_btn.connect("clicked", on_edit)
                row.add_suffix(edit_btn)

                del_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                del_btn.add_css_class("destructive-action")
                def on_del(_, a_idx=idx):
                    automations.pop(a_idx)
                    save_config(self.cfg)
                    self.schedule_auto()
                    rebuild_list()
                del_btn.connect("clicked", on_del)
                row.add_suffix(del_btn)

                group.add(row)

        rebuild_list()
        add_btn.connect("clicked", lambda *_: (dlg.close(), self.show_edit_automation_dialog(None)))

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroll)
        dlg.set_child(view)
        dlg.present(self)

    def show_edit_automation_dialog(self, auto_idx):
        automations = self.cfg.get("automations", [])
        is_new = auto_idx is None or auto_idx >= len(automations)
        auto = automations[auto_idx] if not is_new else {
            "name": f"Automation {len(automations)+1}",
            "enabled": True,
            "direction": "local_to_server",
            "local_folder": "",
            "target": "/",
            "minutes": 15,
            "account_idx": 0,
            "source_account_idx": 0,
            "source_path": "/"
        }

        accounts = self.cfg.get("accounts", [])
        acc_names = [a.get("name", f"Account {i+1}") for i, a in enumerate(accounts)] if accounts else ["Standard"]

        dlg = Adw.Dialog(title="Automation bearbeiten", content_width=650, content_height=750)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Automation"))
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda *_: (dlg.close(), self.show_automations_dialog()))
        save = Gtk.Button(label="Speichern")
        save.add_css_class("suggested-action")
        header.pack_start(cancel)
        header.pack_end(save)

        page = Adw.PreferencesPage()

        g_basic = Adw.PreferencesGroup(title="Grunddaten")
        name_row = Adw.EntryRow(title="Name")
        name_row.set_text(auto.get("name", ""))
        g_basic.add(name_row)

        dir_row = Adw.ComboRow(title="Richtung / Synchronisation")
        dir_model = Gtk.StringList.new(["Lokal -> Server (Upload)", "Server -> Lokal (Download)", "Cloud -> Cloud (WebDAV -> WebDAV)"])
        dir_row.set_model(dir_model)
        cur_dir = auto.get("direction", "local_to_server")
        dir_idx = 0 if cur_dir == "local_to_server" else (1 if cur_dir == "server_to_local" else 2)
        dir_row.set_selected(dir_idx)
        g_basic.add(dir_row)

        spin = Adw.SpinRow.new_with_range(1, 1440, 1)
        spin.set_title("Intervall (Minuten)")
        spin.set_value(auto.get("minutes", 15))
        g_basic.add(spin)
        page.add(g_basic)

        def make_path_row(title, initial_val, get_acc_idx):
            row = Adw.ActionRow(title=title)
            entry = Gtk.Entry(text=initial_val, valign=Gtk.Align.CENTER, hexpand=True)
            row.add_suffix(entry)
            btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Server-Ordner auswählen")
            def on_pick(_):
                accs = self.cfg.get("accounts", [])
                acc_i = get_acc_idx()
                if not accs or not (0 <= acc_i < len(accs)):
                    self.say("Bitte zuerst ein Konto auswählen")
                    return
                acc = accs[acc_i]
                try:
                    client = DavClient(acc["url"], acc["user"], acc["password"])
                except Exception as e:
                    self.say(self.error_text(e))
                    return
                ServerFolderPickerDialog(self, client, entry.get_text() or "/", lambda p: entry.set_text(p)).present(self)
            btn.connect("clicked", on_pick)
            row.add_suffix(btn)
            return row, entry

        g_local = Adw.PreferencesGroup(title="WebDAV-Server &amp; Ordner")
        acc_row = Adw.ComboRow(title="WebDAV-Konto (Server)")
        acc_row.set_model(Gtk.StringList.new(acc_names))
        acc_row.set_selected(auto.get("account_idx", 0))
        g_local.add(acc_row)

        folder_holder = {"folder": auto.get("folder") or auto.get("local_folder", "")}
        folder_row = Adw.ActionRow(title="Lokaler Ordner", subtitle=folder_holder["folder"] or "Nicht ausgewählt", activatable=True)
        folder_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        g_local.add(folder_row)

        target_row, target_entry = make_path_row("Zielordner auf Server", auto.get("target", "/"), lambda: acc_row.get_selected())
        g_local.add(target_row)
        page.add(g_local)

        g_cloud = Adw.PreferencesGroup(title="Cloud zu Cloud Einstellungen")
        src_acc_row = Adw.ComboRow(title="Quell-WebDAV-Konto")
        src_acc_row.set_model(Gtk.StringList.new(acc_names))
        src_acc_row.set_selected(auto.get("source_account_idx", 0))
        g_cloud.add(src_acc_row)

        src_path_row, src_path_entry = make_path_row("Quell-Pfad auf Server", auto.get("source_path", "/"), lambda: src_acc_row.get_selected())
        g_cloud.add(src_path_row)

        tgt_acc_row = Adw.ComboRow(title="Ziel-WebDAV-Konto")
        tgt_acc_row.set_model(Gtk.StringList.new(acc_names))
        tgt_acc_row.set_selected(auto.get("account_idx", 0))
        g_cloud.add(tgt_acc_row)

        tgt_path_row, tgt_path_entry = make_path_row("Ziel-Pfad auf Server", auto.get("target", "/"), lambda: tgt_acc_row.get_selected())
        g_cloud.add(tgt_path_row)
        page.add(g_cloud)

        def on_dir_changed(*_):
            idx = dir_row.get_selected()
            g_local.set_visible(idx != 2)
            g_cloud.set_visible(idx == 2)
        dir_row.connect("notify::selected", on_dir_changed)
        on_dir_changed()

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(page)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(scroll)
        dlg.set_child(view)

        def pick_folder(*_):
            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                folder_holder["folder"] = f.get_path()
                folder_row.set_subtitle(f.get_path())
            Gtk.FileDialog(title="Lokalen Ordner wählen").select_folder(self, None, done)
        folder_row.connect("activated", pick_folder)

        def do_save(*_):
            idx = dir_row.get_selected()
            auto["name"] = name_row.get_text().strip() or "Automation"
            if idx == 0:
                auto["direction"] = "local_to_server"
                auto["account_idx"] = acc_row.get_selected()
            elif idx == 1:
                auto["direction"] = "server_to_local"
                auto["account_idx"] = acc_row.get_selected()
            else:
                auto["direction"] = "cloud_to_cloud"
                auto["source_account_idx"] = src_acc_row.get_selected()
                auto["account_idx"] = tgt_acc_row.get_selected()
                p_src = src_path_entry.get_text().strip().strip("/")
                auto["source_path"] = f"/{p_src}/" if p_src else "/"
                p_tgt = tgt_path_entry.get_text().strip().strip("/")
                auto["target"] = f"/{p_tgt}/" if p_tgt else "/"

            auto["local_folder"] = folder_holder["folder"]
            auto["folder"] = folder_holder["folder"]
            if idx != 2:
                p = target_entry.get_text().strip().strip("/")
                auto["target"] = f"/{p}/" if p else "/"

            auto["minutes"] = int(spin.get_value())
            auto["enabled"] = True
            if is_new:
                automations.append(auto)
            else:
                automations[auto_idx] = auto
            self.cfg["automations"] = automations
            save_config(self.cfg)
            self.schedule_auto()
            dlg.close()
            self.show_automations_dialog()
        save.connect("clicked", do_save)

        dlg.present(self)

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.cassowary.Client")

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))