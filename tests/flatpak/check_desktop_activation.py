#!/usr/bin/python3
"""Exercise image D-Bus activation on a private bus, never the host system bus."""

from pathlib import Path
import subprocess
import tempfile

import dbus


with tempfile.TemporaryDirectory(prefix="desktop-activation-") as directory:
    root = Path(directory)
    services = root / "services"
    services.mkdir()
    # A deliberately failing service proves the bus actually attempts Exec
    # activation. Otherwise an incorrectly configured bus could pass the test.
    (services / "org.mertensia.PolicyTest.service").write_text(
        "[D-BUS Service]\nName=org.mertensia.PolicyTest\nExec=/usr/bin/false\nUser=root\n")
    config = root / "bus.conf"
    config.write_text(f'''<busconfig>
  <type>system</type>
  <listen>unix:tmpdir={root}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{services}</servicedir>
  <servicedir>/etc/dbus-1/system-services</servicedir>
  <servicedir>/usr/local/share/dbus-1/system-services</servicedir>
  <servicedir>/usr/share/dbus-1/system-services</servicedir>
  <policy context="default">
    <allow user="*"/><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/>
  </policy>
</busconfig>''')
    daemon = subprocess.Popen(["dbus-daemon", "--nofork", "--print-address=1",
                               f"--config-file={config}"], stdout=subprocess.PIPE, text=True)
    try:
        address = daemon.stdout.readline().strip()
        if not address:
            raise RuntimeError("Private bus did not start")
        bus = dbus.bus.BusConnection(address)
        manager = dbus.Interface(bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                                 "org.freedesktop.DBus")
        try:
            manager.StartServiceByName("org.mertensia.PolicyTest", dbus.UInt32(0), timeout=10)
        except dbus.DBusException as error:
            if error.get_dbus_name() != "org.freedesktop.DBus.Error.Spawn.ChildExited":
                raise
        else:
            raise RuntimeError("Control activation unexpectedly succeeded")
        try:
            manager.StartServiceByName("org.rpm.dnf.v0", dbus.UInt32(0), timeout=10)
        except dbus.DBusException as error:
            if error.get_dbus_name() != "org.freedesktop.DBus.Error.ServiceUnknown":
                raise
        else:
            raise RuntimeError("DNF D-Bus activation unexpectedly succeeded")
        bus.close()
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)
print("Private system bus: control activation attempted; DNF activation unavailable")
