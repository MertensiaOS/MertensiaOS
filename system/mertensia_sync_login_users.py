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
    requested = {}
    for home in homes.ListHomes():
        # Match native AccountsService enumeration: a registered home on
        # disconnected storage is intentionally absent from the login list.
        if str(home[2]) == "absent":
            continue
        username = str(home[0])
        try:
            # Older AccountsService versions need explicit NSS caching. New
            # versions enumerate homed directly and CacheUser is a no-op.
            # Neither path creates a passwd entry or unlocks the home.
            requested[username] = accounts.CacheUser(username)
        except dbus.DBusException as error:
            print(f"Unable to cache login user {username}: {error}", file=sys.stderr)
            failed = True
    cached = set(accounts.ListCachedUsers())
    for username, path in requested.items():
        if path not in cached:
            print(f"Login user {username} is absent from AccountsService's login list", file=sys.stderr)
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
