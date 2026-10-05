#!/usr/bin/python3
"""Expose homed users to the AccountsService cache used by GDM."""

import sys

import dbus


def sync_users(bus):
    homes = dbus.Interface(
        bus.get_object("org.freedesktop.home1", "/org/freedesktop/home1"),
        "org.freedesktop.home1.Manager",
    )
    accounts = dbus.Interface(
        bus.get_object("org.freedesktop.Accounts", "/org/freedesktop/Accounts"),
        "org.freedesktop.Accounts",
    )
    failed = False
    for home in homes.ListHomes():
        username = str(home[0])
        try:
            # CacheUser resolves through NSS and persists the cache entry;
            # it neither creates a passwd entry nor unlocks the home.
            accounts.CacheUser(username)
        except dbus.DBusException as error:
            print(f"Unable to cache login user {username}: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


def main():
    try:
        return sync_users(dbus.SystemBus())
    except dbus.DBusException as error:
        print(f"Unable to synchronize login users: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
