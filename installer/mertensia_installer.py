#!/usr/bin/python3
"""GTK front end for the guided MertensiaOS whole-disk installer."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402


HELPER = "/usr/libexec/mertensia-installer-helper"
BRANDING = Path(__file__).resolve().parent / "branding"
if not BRANDING.is_dir():
    BRANDING = Path("/usr/share/mertensia-installer/branding")


def load_brand_style(display: Gdk.Display, dark: bool, high_contrast: bool = False) -> Gtk.CssProvider:
    """Keep native high-contrast colors; use Quiet Bloom roles otherwise."""
    provider = Gtk.CssProvider()
    colors = ""
    if not high_contrast:
        theme = json.loads((BRANDING / "brand-tokens.json").read_text())["themes"]["dark" if dark else "light"]
        roles = {
            "window_bg_color": theme["background"], "window_fg_color": theme["text"],
            "view_bg_color": theme["surface"], "view_fg_color": theme["text"],
            "card_bg_color": theme["surface"], "card_fg_color": theme["text"],
            "headerbar_bg_color": theme["background"], "headerbar_fg_color": theme["text"],
            "accent_bg_color": theme["action"], "accent_fg_color": theme["onAction"],
            "accent_color": theme["link"],
            "brand_muted": "#D0C3DC" if dark else "#65586F",
            "brand_border": "#695777" if dark else "#C9BFD3",
        }
        colors = "\n".join(f"@define-color {name} {value};" for name, value in roles.items())
    else:
        colors = "@define-color brand_muted @window_fg_color; @define-color brand_border @window_fg_color;"
    provider.load_from_data((colors + "\n" + (BRANDING / "installer.css").read_text()).encode())
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return provider


def human_size(size: int) -> str:
    return f"{size / 1024**3:.1f} GiB"


class InstallerWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Install MertensiaOS")
        self.set_default_size(1000, 760)
        self.add_css_class("installer")
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(str(BRANDING / "icons"))
        self.fullscreen()
        self.process: subprocess.Popen[str] | None = None
        self.disks: list[dict] = []
        self.recovery_key = ""
        self.last_phase = "Starting installation"
        self.install_disk = ""
        self.failure_report = ""

        self.style_manager = Adw.StyleManager.get_default()
        self.css_provider = None
        self._update_style()
        self.style_manager.connect("notify::dark", self._update_style)
        self.style_manager.connect("notify::high-contrast", self._update_style)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="MertensiaOS", css_classes=["brand-name"]))
        header.add_css_class("flat")
        toolbar.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=180, vhomogeneous=False, hhomogeneous=False)
        self.stack.connect("notify::visible-child-name", self._page_changed)
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        self.brand_marks = []
        self._build_welcome()
        self._build_requirements()
        self._build_disk()
        self._build_confirm()
        self._build_progress()
        self._build_recovery()
        self._build_finish()
        self._build_failure()
        self._update_brand_marks()

    def _update_style(self, *_args) -> None:
        if self.css_provider:
            Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.css_provider)
        self.css_provider = load_brand_style(
            self.get_display(), self.style_manager.get_dark(), self.style_manager.get_high_contrast()
        )
        self._update_brand_marks()

    def _update_brand_marks(self) -> None:
        variant = "tint" if self.style_manager.get_dark() else "purple"
        for mark in getattr(self, "brand_marks", []):
            mark.set_from_file(str(BRANDING / "assets" / f"soft-bloom-{variant}.svg"))

    def _page_changed(self, *_args) -> None:
        page = self.stack.get_visible_child()
        if page and hasattr(page, "heading"):
            page.heading.grab_focus()

    def _page(self, title: str, body: str, *, step: str, icon: str, brand: bool = False) -> tuple[Gtk.ScrolledWindow, Gtk.Box]:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=688, tightening_threshold=520, unit=Adw.LengthUnit.PX)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
                        margin_top=24, margin_bottom=32, margin_start=24, margin_end=24,
                        valign=Gtk.Align.CENTER, css_classes=["page"])
        intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        badge = Gtk.Image.new_from_icon_name(icon)
        badge.set_halign(Gtk.Align.START)
        if brand:
            badge.set_pixel_size(64)
            badge.add_css_class("brand-mark")
            self.brand_marks.append(badge)
        else:
            badge.set_pixel_size(32)
            badge.add_css_class("page-icon")
            if icon == "mertensia-warning-symbolic":
                badge.add_css_class("warning-icon")
            elif icon == "mertensia-check-symbolic":
                badge.add_css_class("success-icon")
        intro.append(badge)
        intro.append(Gtk.Label(label=step, xalign=0, css_classes=["step-label"]))
        heading = Gtk.Label(label=title, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                            css_classes=["page-title"], focusable=True, accessible_role=Gtk.AccessibleRole.HEADING)
        intro.append(heading)
        intro.append(Gtk.Label(label=body, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                               css_classes=["page-description"]))
        outer.append(intro)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.append(content)
        clamp.set_child(outer)
        scroll.set_child(clamp)
        heading.connect("map", lambda widget: widget.grab_focus())
        scroll.heading = heading
        return scroll, content

    def _buttons(self, *buttons: Gtk.Button) -> Gtk.FlowBox:
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                          column_spacing=12, row_spacing=12, min_children_per_line=1,
                          max_children_per_line=len(buttons), margin_top=8)
        row.add_css_class("actions")
        for button in buttons:
            button.add_css_class("action-button")
            row.insert(button, -1)
        return row

    def _info_row(self, icon: str, title: str, body: str) -> Gtk.Box:
        row = Gtk.Box(spacing=16, css_classes=["info-row"])
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(24)
        image.set_valign(Gtk.Align.START)
        image.add_css_class("info-icon")
        if icon == "mertensia-warning-symbolic":
            image.add_css_class("warning-icon")
        row.append(image)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        text.append(Gtk.Label(label=title, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, css_classes=["heading"]))
        text.append(Gtk.Label(label=body, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              css_classes=["secondary-text"]))
        row.append(text)
        return row

    def _build_welcome(self) -> None:
        page, content = self._page(
            "Welcome to MertensiaOS",
            "A few guided steps to set up your system. Before you begin, connect to power and back up anything you want to keep.",
            step="Let’s get you set up", icon="mertensia-install-symbolic", brand=True,
        )
        overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["overview-card"])
        for icon, title, body in (
            ("mertensia-computer-symbolic", "Check your computer", "We’ll check UEFI, Secure Boot and TPM 2.0 before you continue."),
            ("mertensia-disk-symbolic", "Choose a disk", "Installation uses the entire disk and erases its contents."),
            ("mertensia-key-symbolic", "Keep your recovery key", "Save a copy outside this live system before finishing."),
        ):
            overview.append(self._info_row(icon, title, body))
        content.append(overview)
        start = Gtk.Button(label="Start installation", css_classes=["suggested-action"])
        start.connect("clicked", lambda *_: self._probe())
        close = Gtk.Button(label="Close installer", css_classes=["flat"])
        close.connect("clicked", lambda *_: self.close())
        content.append(self._buttons(close, start))
        self.stack.add_named(page, "welcome")

    def _build_requirements(self) -> None:
        page, content = self._page("Checking this computer", "MertensiaOS requires an x86-64 UEFI system with Secure Boot and TPM 2.0 enabled.", step="Step 1 of 5 · Check your computer", icon="mertensia-computer-symbolic")
        self.requirements_heading = page.heading
        self.requirements_label = Gtk.Label(wrap=True, justify=Gtk.Justification.LEFT, xalign=0)
        self.requirements_label.add_css_class("secondary-text")
        content.append(self.requirements_label)
        self.requirement_rows = {}
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["overview-card"])
        for key, name in (("architecture_ok", "x86-64 processor"), ("uefi", "UEFI boot"),
                          ("secure_boot", "Secure Boot"), ("tpm2", "TPM 2.0")):
            row = self._info_row("mertensia-pending-symbolic", name, "Waiting for check")
            self.requirement_rows[key] = (row.get_first_child(), row.get_last_child().get_last_child())
            checks.append(row)
        content.append(checks)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self.stack.set_visible_child_name("welcome"))
        self.requirements_next = Gtk.Button(label="Choose a disk", sensitive=False, css_classes=["suggested-action"])
        self.requirements_next.connect("clicked", lambda *_: self.stack.set_visible_child_name("disk"))
        content.append(self._buttons(back, self.requirements_next))
        self.stack.add_named(page, "requirements")

    def _build_disk(self) -> None:
        page, content = self._page("Choose the installation disk", "Choose where MertensiaOS will be installed. Dual boot and manual partitioning are not supported.", step="Step 2 of 5 · Choose a disk", icon="mertensia-disk-symbolic")
        self.disk_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["boxed-list"])
        self.disk_list.connect("row-selected", lambda _list, row: self.disk_next.set_sensitive(row is not None))
        content.append(self._info_row("mertensia-warning-symbolic", "The entire disk will be erased",
                                      "Back up your files before continuing. You’ll confirm your choice in the next step."))
        self.disk_status = Gtk.Label(label="Looking for available disks…", wrap=True, xalign=0,
                                     css_classes=["secondary-text"])
        content.append(self.disk_status)
        content.append(self.disk_list)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self.stack.set_visible_child_name("requirements"))
        self.disk_next = Gtk.Button(label="Continue", sensitive=False, css_classes=["suggested-action"])
        self.disk_next.connect("clicked", lambda *_: self._show_confirmation())
        content.append(self._buttons(back, self.disk_next))
        self.stack.add_named(page, "disk")

    def _build_confirm(self) -> None:
        page, content = self._page("Confirm disk erasure", "Everything on this disk will be permanently erased, including files and other operating systems. This cannot be undone.", step="Step 3 of 5 · Confirm your choice", icon="mertensia-warning-symbolic")
        self.confirm_disk = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, css_classes=["disk-summary"])
        content.append(self.confirm_disk)
        erase_label = Gtk.Label(label="Type ERASE to confirm", xalign=0, css_classes=["heading"])
        content.append(erase_label)
        self.erase_entry = Gtk.Entry(placeholder_text="ERASE")
        self.erase_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Type ERASE to confirm"])
        self.erase_entry.connect("changed", lambda entry: self.install_button.set_sensitive(entry.get_text() == "ERASE"))
        content.append(self.erase_entry)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda *_: self.stack.set_visible_child_name("disk"))
        self.install_button = Gtk.Button(label="Erase disk and install", sensitive=False, css_classes=["destructive-action"])
        self.install_button.connect("clicked", lambda *_: self._install())
        content.append(self._buttons(back, self.install_button))
        self.stack.add_named(page, "confirm")

    def _build_progress(self) -> None:
        page, content = self._page("Installing MertensiaOS", "Keep this computer connected to power. You’ll save a recovery key before setup completes.", step="Step 4 of 5 · Install the system", icon="mertensia-install-symbolic")
        self.progress_label = Gtk.Label(label="Starting…", wrap=True)
        self.progress = Gtk.ProgressBar(show_text=True, text="Preparing installation…", css_classes=["install-progress"])
        content.append(self.progress_label)
        content.append(self.progress)
        self.stack.add_named(page, "progress")

    def _build_recovery(self) -> None:
        page, content = self._page("Save your root recovery key", "Use this key if a firmware or Secure Boot change prevents the TPM from unlocking the system. It is shown only once.", step="Step 5 of 5 · Save your recovery key", icon="mertensia-key-symbolic")
        self.recovery_label = Gtk.Label(selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                        justify=Gtk.Justification.CENTER,
                                        css_classes=["recovery-key", "monospace"])
        content.append(self.recovery_label)
        save = Gtk.Button(label="Save key to a file")
        save.connect("clicked", self._save_recovery)
        copy = Gtk.Button(label="Copy key")
        copy.connect("clicked", self._copy_recovery)
        content.append(self._buttons(copy, save))
        self.recovery_check = Gtk.CheckButton()
        self.recovery_check.set_child(Gtk.Label(label="I have saved or written down this key outside the live system",
                                               wrap=True, xalign=0, hexpand=True))
        self.recovery_check.connect("toggled", self._recovery_toggled)
        content.append(self.recovery_check)
        self.recovery_continue = Gtk.Button(label="Finish installation", sensitive=False, css_classes=["suggested-action"])
        self.recovery_continue.connect("clicked", self._confirm_recovery)
        content.append(self._buttons(self.recovery_continue))
        self.stack.add_named(page, "recovery")

    def _build_finish(self) -> None:
        page, content = self._page("Installation complete", "Restart the computer and remove the installation media. On first boot, you’ll create your encrypted user account.", step="Ready for your first boot", icon="mertensia-check-symbolic")
        restart = Gtk.Button(label="Restart", css_classes=["suggested-action"])
        restart.connect("clicked", lambda *_: subprocess.Popen(["systemctl", "reboot"]))
        close = Gtk.Button(label="Close installer", css_classes=["flat"])
        close.connect("clicked", lambda *_: self.close())
        content.append(self._buttons(close, restart))
        self.stack.add_named(page, "finish")

    def _helper_once(self, command: str, callback) -> None:
        def worker():
            try:
                result = subprocess.run(["pkexec", HELPER, command], text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                message = json.loads(result.stdout.splitlines()[-1])
            except Exception as error:
                message = {"event": "error", "message": str(error)}
            GLib.idle_add(callback, message)
        threading.Thread(target=worker, daemon=True).start()

    def _build_failure(self) -> None:
        page, content = self._page(
            "Installation stopped",
            "Installation did not complete. The selected disk may be partially modified and may not boot.",
            step="Installation needs attention", icon="mertensia-warning-symbolic",
        )
        self.failure_context = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, css_classes=["heading"])
        self.failure_steps = Gtk.Label(wrap=True, xalign=0)
        self.failure_details = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True, xalign=0, css_classes=["monospace", "error-details"])
        content.append(self.failure_context)
        content.append(Gtk.Label(label="What to do next", xalign=0, css_classes=["heading"]))
        content.append(self.failure_steps)
        content.append(Gtk.Label(label="Error details", xalign=0, css_classes=["heading"]))
        content.append(self.failure_details)
        copy = Gtk.Button(label="Copy error details")
        copy.connect("clicked", lambda *_: self.get_clipboard().set(self.failure_report))
        close = Gtk.Button(label="Close installer", css_classes=["suggested-action"])
        close.connect("clicked", lambda *_: self.close())
        restart = Gtk.Button(label="Restart")
        restart.connect("clicked", lambda *_: subprocess.Popen(["systemctl", "reboot"]))
        content.append(self._buttons(copy, close, restart))
        self.stack.add_named(page, "failure")

    def _show_failure(self, message: str) -> None:
        # Keep the original diagnostic if a subsequent process-exit event arrives.
        if self.failure_report:
            return
        lower = message.lower()
        retained = "recovery state" in lower and "no encrypted volume was created" not in lower
        if retained:
            advice = "Keep this live session running. Use the recovery command in the error details to save your key or finish installation before restarting."
        elif "in use" in lower or "busy" in lower or "already mapped" in lower:
            advice = "Restart into the installation media to release active disks. Before trying again, leave the target disk unopened in Files and other applications."
        elif "memory" in lower or "cannot allocate" in lower:
            advice = "Close other applications or give the virtual machine more RAM, then restart into the installation media before trying again."
        elif "tpm" in lower:
            advice = "Check that TPM 2.0 and Secure Boot are enabled in the computer or virtual machine settings, then restart into the installation media."
        else:
            advice = "Restart into the installation media before trying again. If the error repeats, include the details below when reporting the problem."
        context = f"Stopped during: {self.last_phase}\nSelected disk: {self.install_disk}"
        steps = (
            "1. Keep this computer running; the retained keys are lost when the live system restarts.\n\n"
            f"2. {advice}\n\n"
            "3. Copy the error details and save them outside this live system."
        ) if retained else (
            "1. Copy the error details and save them outside the live system before restarting.\n\n"
            f"2. {advice}\n\n"
            "3. Keep the installation media connected when restarting. A new installation attempt will erase the selected disk again."
        )
        self.failure_report = f"MertensiaOS installation failed\n{context}\n\n{message}"
        self.failure_context.set_text(context)
        self.failure_steps.set_text(steps)
        self.failure_details.set_text(message)
        self.recovery_continue.set_sensitive(False)
        self.stack.set_visible_child_name("failure")

    def _probe(self) -> None:
        self.stack.set_visible_child_name("requirements")
        self.requirements_heading.set_text("Checking this computer")
        self.requirements_label.set_text("Checking hardware…")
        self.requirements_next.set_sensitive(False)
        for icon, status in self.requirement_rows.values():
            icon.remove_css_class("success-icon")
            icon.remove_css_class("warning-icon")
            icon.set_from_icon_name("mertensia-pending-symbolic")
            status.set_text("Checking…")
        self._helper_once("probe", self._probe_done)

    def _probe_done(self, data: dict) -> bool:
        if data.get("event") == "error":
            self.requirements_heading.set_text("Could not check this computer")
            self.requirements_label.set_text(data.get("message", "Hardware check failed."))
            for icon, status in self.requirement_rows.values():
                icon.add_css_class("warning-icon")
                icon.set_from_icon_name("mertensia-warning-symbolic")
                status.set_text("Could not check")
            return False
        for key, (icon, status) in self.requirement_rows.items():
            ok = bool(data.get(key))
            icon.remove_css_class("success-icon")
            icon.remove_css_class("warning-icon")
            icon.add_css_class("success-icon" if ok else "warning-icon")
            icon.set_from_icon_name("mertensia-check-symbolic" if ok else "mertensia-warning-symbolic")
            status.set_text("Ready" if ok else "Requirement not met")
        self.requirements_heading.set_text("Your computer is ready" if data.get("ok") else "Check your computer settings")
        self.requirements_label.set_text(
            "Your computer is ready. Continue to choose an installation disk." if data.get("ok")
            else "Some requirements are not met. Check your firmware settings before trying again."
        )
        self.requirements_next.set_sensitive(bool(data.get("ok")))
        if data.get("ok"):
            self._refresh_disks()
        return False

    def _refresh_disks(self) -> None:
        self.disk_next.set_sensitive(False)
        self.disk_list.set_sensitive(False)
        self.disk_status.set_text("Looking for available disks…")
        self._helper_once("list-disks", self._disks_done)

    def _disks_done(self, data: dict) -> bool:
        self.disk_list.set_sensitive(True)
        while child := self.disk_list.get_first_child():
            self.disk_list.remove(child)
        self.disks = data.get("disks", []) if data.get("event") == "disks" else []
        self.disk_next.set_sensitive(False)
        self.disk_status.set_text(
            "Select a disk to continue." if self.disks else
            data.get("message", "No eligible disk of at least 24 GiB was found. Connect a disk, then return to the welcome screen to check again.")
        )
        for index, disk in enumerate(self.disks):
            details = f"{human_size(disk['size'])} · {disk['stable_path']}"
            if disk.get("serial"):
                details += f"\nSerial: {disk['serial']}"
            row = Gtk.ListBoxRow()
            row.disk_index = index
            row.set_child(self._info_row("mertensia-disk-symbolic", disk['model'], details))
            self.disk_list.append(row)
        return False

    def _selected_disk(self) -> dict:
        row = self.disk_list.get_selected_row()
        return self.disks[row.disk_index]

    def _show_confirmation(self) -> None:
        disk = self._selected_disk()
        self.confirm_disk.set_text(f"Erase {disk['model']} ({human_size(disk['size'])}) at {disk['stable_path']}")
        self.erase_entry.set_text("")
        self.stack.set_visible_child_name("confirm")

    def _install(self) -> None:
        disk = self._selected_disk()
        self.install_disk = f"{disk['model']} ({human_size(disk['size'])}) at {disk['stable_path']}"
        self.last_phase = "Starting installation"
        self.failure_report = ""
        self.stack.set_visible_child_name("progress")
        argv = ["pkexec", HELPER, "install", "--disk", disk["stable_path"]]
        try:
            self.process = subprocess.Popen(argv, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1)
        except OSError as error:
            self._show_failure(f"Could not start the installer: {error}")
            return
        threading.Thread(target=self._read_events, daemon=True).start()

    def _read_events(self) -> None:
        assert self.process and self.process.stdout
        terminal_event = False
        for line in self.process.stdout:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("event") in ("error", "complete"):
                terminal_event = True
            GLib.idle_add(self._event, data)
        code = self.process.wait()
        if not terminal_event:
            GLib.idle_add(self._event, {"event": "error", "message": f"The installer stopped without reporting completion (exit {code})."})

    def _event(self, data: dict) -> bool:
        event = data.get("event")
        if event == "phase":
            self.last_phase = data.get("message", "Installing…")
            self.progress_label.set_text(self.last_phase)
            fraction = max(0.0, min(1.0, float(data.get("progress", 0))))
            self.progress.set_fraction(fraction)
            self.progress.set_text(f"{fraction:.0%} complete")
        elif event == "recovery-key":
            self.recovery_key = data["key"]
            self.recovery_label.set_text(self.recovery_key)
            self.stack.set_visible_child_name("recovery")
        elif event == "complete":
            self.stack.set_visible_child_name("finish")
        elif event == "error":
            self._show_failure(data.get("message", "Unknown error"))
        return False

    def _copy_recovery(self, *_args) -> None:
        self.get_clipboard().set(self.recovery_key)

    def _save_recovery(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Save to removable media", initial_name="mertensia-root-recovery-key.txt")
        media = Path("/run/media") / os.environ.get("USER", "mertensia-installer")
        if media.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(media)))
        dialog.save(self, None, self._save_done)

    def _save_done(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            file = dialog.save_finish(result)
            path = file.get_path()
            if not path:
                return
            resolved = os.path.realpath(path)
            if not (resolved.startswith("/run/media/") or resolved.startswith("/media/")):
                warning = Adw.AlertDialog(
                    heading="Choose removable media",
                    body="Save the key to a mounted USB drive under /run/media or /media so it remains available after restarting.",
                )
                warning.add_response("close", "Close")
                warning.present(self)
                return
            Path(path).write_text(
                "MertensiaOS root recovery key\n\n" + self.recovery_key + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # FAT-formatted removable media cannot represent Unix modes.
        except (GLib.Error, OSError) as error:
            warning = Adw.AlertDialog(heading="Could not save the key", body=str(error))
            warning.add_response("close", "Close")
            warning.present(self)

    def _recovery_toggled(self, button: Gtk.CheckButton) -> None:
        self.recovery_continue.set_sensitive(button.get_active())

    def _confirm_recovery(self, *_args) -> None:
        if self.process and self.process.stdin:
            self.stack.set_visible_child_name("progress")
            self.progress_label.set_text("Finalizing installation…")
            try:
                self.process.stdin.write("confirm-recovery\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                self._show_failure(
                    f"Could not confirm the saved recovery key: {error}. "
                    "Recovery state may remain in this live session. Keep this computer running and use "
                    "the installer helper's resume command before restarting."
                )
                return
            self.recovery_key = ""


class InstallerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.mertensia.Installer")

    def do_activate(self):
        window = self.get_active_window() or InstallerWindow(self)
        window.fullscreen()
        window.present()


if __name__ == "__main__":
    raise SystemExit(InstallerApp().run())
