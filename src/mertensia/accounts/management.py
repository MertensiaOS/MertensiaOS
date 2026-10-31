"""Authorization and invariants for administrative changes to homed accounts."""
from __future__ import annotations

from .validation import RequestError, SETUP_USER, USERNAME_RE


def manage_account(request, caller, operations):
    operation = request.get("operation")
    username = request.get("username")
    if not isinstance(operation, str) or operation not in {"set-admin", "set-locked", "remove"}:
        raise RequestError("unsupported account operation")
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username) or username == SETUP_USER:
        raise RequestError("username is invalid")
    record = operations.home_record(username)
    if record is None:
        raise RequestError("the encrypted account no longer exists")
    if operation != "remove" and not isinstance(request.get("enabled"), bool):
        raise RequestError("enabled must be a boolean")
    enabled = request.get("enabled", False)
    if operation in {"remove", "set-locked"} and username == caller.username and (
        operation == "remove" or enabled
    ):
        raise RequestError("you cannot remove or disable the account you are using")
    removes_admin = (operation == "remove" or operation == "set-admin" and not enabled
                     or operation == "set-locked" and enabled)
    if removes_admin and "wheel" in record.get("memberOf", []) and not record.get("locked", False):
        others = [operations.home_record(name) for name in operations.home_names() if name != username]
        if not any(user and "wheel" in user.get("memberOf", []) and not user.get("locked", False) for user in others):
            raise RequestError("keep at least one enabled administrator account")
    if operation == "remove":
        if request.get("confirm_username") != username:
            raise RequestError("type the username to confirm permanent deletion")
        if operations.home_in_use(username):
            raise RequestError("this user must sign out before their account can be removed")
        operations.manager.RemoveHome(username, timeout=120)
    elif operation == "set-admin":
        groups = [group for group in record.get("memberOf", []) if group != "wheel"]
        if enabled:
            groups.append("wheel")
        operations.update_properties(username, ["--member-of=" + group for group in groups] or ["--member-of="])
    else:
        operations.update_properties(username, ["--locked=" + ("yes" if enabled else "no")])
    return username
