#!/usr/bin/python3
"""Disposable guest-only Chromium checks; CDP uses private pipes, never TCP."""

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time


POLICIES = {
    "mandatory": {"SitePerProcess": True, "MetricsReportingEnabled": False,
                  "UrlKeyedAnonymizedDataCollectionEnabled": False},
    "recommended": {"SafeBrowsingProtectionLevel": 1, "BackgroundModeEnabled": False,
                    "SearchSuggestEnabled": False, "NetworkPredictionOptions": 2,
                    "SpellCheckServiceEnabled": False},
}


def validate_policies(export):
    """Fail closed on schema drift, ignored policies, or wrong enforcement."""
    names = set().union(*POLICIES.values())

    def find(value):
        if isinstance(value, dict):
            if names <= value.keys():
                yield value
            for child in value.values():
                yield from find(child)
        elif isinstance(value, list):
            for child in value:
                yield from find(child)

    candidates = list(find(export))
    if len(candidates) != 1:
        raise RuntimeError("Chromium policy export is missing or ambiguous")
    policies = candidates[0]
    for name, entry in policies.items():
        if any(entry.get(key) for key in ("error", "warning", "ignored", "unknown", "deprecated", "conflicts")):
            raise RuntimeError(f"Chromium rejected or conflicted policy: {name}")
    for level, expected in POLICIES.items():
        for name, value in expected.items():
            entry = policies[name]
            if (type(entry.get("value")) is not type(value) or entry["value"] != value
                    or entry.get("level") != level or entry.get("scope") != "machine"
                    or entry.get("source") != "platform"):
                raise RuntimeError(f"Chromium policy has incorrect value or enforcement: {name}")
    return sorted(names)


def validate_sandbox(status):
    for key in ("userNs", "pidNs", "netNs", "seccompBpf", "seccompTsync"):
        if status.get(key) is not True:
            raise RuntimeError(f"Chromium native sandbox protection is inactive: {key}")
    if status.get("suid") is not False:
        raise RuntimeError("Chromium unexpectedly used the SUID sandbox")


class Browser:
    def __init__(self, profile):
        self.sequence = 0
        self.buffer = b""
        command_read, self.command_write = os.pipe()
        self.response_read, response_write = os.pipe()
        self.log = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--browser",
                 str(command_read), str(response_write), str(profile)],
                pass_fds=(command_read, response_write), stdin=subprocess.DEVNULL,
                stdout=self.log, stderr=self.log, start_new_session=True,
            )
        finally:
            os.close(command_read)
            os.close(response_write)

    def call(self, method, params=None, session=None):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        os.write(self.command_write, json.dumps(request).encode() + b"\0")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b"\0" in self.buffer:
                message, self.buffer = self.buffer.split(b"\0", 1)
                reply = json.loads(message)
                if reply.get("id") == self.sequence:
                    if "error" in reply:
                        raise RuntimeError(f"Chromium {method} failed: {reply['error']}")
                    return reply.get("result", {})
            if select.select([self.response_read], [], [], max(0, deadline - time.monotonic()))[0]:
                data = os.read(self.response_read, 65536)
                if not data:
                    self.log.seek(0)
                    raise RuntimeError("Chromium closed its diagnostic pipe: " + self.log.read().decode(errors="replace")[-3000:])
                self.buffer += data
        raise RuntimeError(f"Chromium {method} timed out")

    def evaluate(self, session, expression):
        reply = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                              "awaitPromise": True}, session)
        if "exceptionDetails" in reply:
            raise RuntimeError(f"Chromium diagnostic evaluation failed: {reply['exceptionDetails']}")
        return reply["result"].get("value")

    def page(self, url):
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.navigate", {"url": url}, session)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if self.evaluate(session, f"location.href.split('#')[0] === {json.dumps(url)} && document.readyState === 'complete'"):
                    return session
            except RuntimeError:
                pass  # Navigation replaces the initial JS execution context.
            time.sleep(0.1)
        raise RuntimeError(f"Chromium did not load {url}")

    def close(self):
        try:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
        finally:
            os.close(self.command_write)
            os.close(self.response_read)
            self.log.close()


def browser_child(command_fd, response_fd, profile):
    # Duplicate above 4 first: a source descriptor can itself be 3 or 4.
    command = fcntl.fcntl(command_fd, fcntl.F_DUPFD_CLOEXEC, 10)
    response = fcntl.fcntl(response_fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.dup2(command, 3)
    os.dup2(response, 4)
    os.execv("/usr/bin/chromium-browser", [
        "/usr/bin/chromium-browser", "--headless=new", "--remote-debugging-pipe",
        "--no-first-run", "--no-default-browser-check", "--lang=en-US",
        "--user-data-dir=" + profile, "about:blank",
    ])


def check_handlers():
    """Exercise GLib's real defaults and user precedence with isolated XDG data."""
    with tempfile.TemporaryDirectory(prefix="mertensia-mime-") as temporary:
        root = Path(temporary)
        config, data = root / "config", root / "data"
        config.mkdir()
        applications = data / "applications"
        applications.mkdir(parents=True)
        environment = dict(os.environ, LC_ALL="C", XDG_CURRENT_DESKTOP="GNOME",
                           XDG_CONFIG_HOME=str(config), XDG_DATA_HOME=str(data),
                           XDG_CONFIG_DIRS="/etc/xdg", XDG_DATA_DIRS="/usr/local/share:/usr/share")
        alternative = "mertensia-browser-alternative.desktop"
        (applications / alternative).write_text(
            "[Desktop Entry]\nType=Application\nName=Disposable alternative\nExec=/usr/bin/true %U\n")
        for mime in ("x-scheme-handler/http", "x-scheme-handler/https", "text/html", "application/xhtml+xml"):
            def selected():
                output = subprocess.run(["gio", "mime", mime], env=environment, check=True,
                                        capture_output=True, text=True, timeout=10).stdout
                return output.splitlines()[0].rsplit(": ", 1)[-1]

            if selected() != "chromium-browser.desktop":
                raise RuntimeError(f"GNOME does not select Chromium by default for {mime}")
            subprocess.run(["gio", "mime", mime, alternative], env=environment, check=True,
                           capture_output=True, timeout=10)
            if selected() != alternative:
                raise RuntimeError(f"System browser default overrides user choice for {mime}")


def probe():
    if os.geteuid() == 0:
        raise RuntimeError("Chromium probe must run as an unprivileged guest user")
    check_handlers()
    with tempfile.TemporaryDirectory(prefix="mertensia-browser-") as profile:
        browser = Browser(profile)
        try:
            policy_page = browser.page("chrome://policy/")
            exported = browser.evaluate(policy_page, """(async () => {
                const {sendWithPromise} = await import('chrome://resources/js/cr.js');
                return await sendWithPromise('getPoliciesJson', 0);
            })()""")
            policies = validate_policies(json.loads(exported) if isinstance(exported, str) else exported)
            sandbox_page = browser.page("chrome://sandbox/")
            sandbox = browser.evaluate(sandbox_page, """(async () => {
                const {loadTimeData} = await import('chrome://resources/js/load_time_data.js');
                return Object.fromEntries(['suid', 'userNs', 'pidNs', 'netNs',
                    'seccompBpf', 'seccompTsync'].map(key => [key, loadTimeData.getBoolean(key)]));
            })()""")
            validate_sandbox(sandbox)
            process_page = browser.page("chrome://process-internals/")
            mode = browser.evaluate(process_page, """(async () => {
                const {ProcessInternalsHandler} = await import('./process_internals.mojom-webui.js');
                return (await ProcessInternalsHandler.getRemote().getIsolationMode()).mode;
            })()""")
            if "site per process" not in mode.lower().replace('-', ' '):
                raise RuntimeError(f"Chromium site isolation is inactive: {mode}")
            return {"policies": policies, "sandbox": sandbox, "site_isolation": mode,
                    "default_handlers": True, "user_default_override": True,
                    "graphical_session_tested": False, "safe_browsing_network_tested": False}
        finally:
            browser.close()


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--browser":
        browser_child(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        print(json.dumps(probe()))
