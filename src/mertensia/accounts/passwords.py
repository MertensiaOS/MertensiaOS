"""Own-password changes through the image-owned homed/oo7 PAM service."""
from __future__ import annotations

import ctypes

from .validation import RequestError


SERVICE = b"mertensia-password"
PAM_SUCCESS = 0
PAM_BUF_ERR = 5
PAM_CONV_ERR = 19


class PamError(RequestError):
    def __init__(self, code: int):
        self.code = code
        # Never expose PAM messages: they can contain backend/user input.
        super().__init__("The password service could not complete this change.")


class Message(ctypes.Structure):
    _fields_ = [("style", ctypes.c_int), ("text", ctypes.c_char_p)]


class Response(ctypes.Structure):
    _fields_ = [("text", ctypes.c_void_p), ("code", ctypes.c_int)]


Callback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                           ctypes.POINTER(ctypes.POINTER(Message)),
                           ctypes.POINTER(ctypes.POINTER(Response)), ctypes.c_void_p)


class Conversation(ctypes.Structure):
    _fields_ = [("callback", Callback), ("data", ctypes.c_void_p)]


def _libc():
    libc = ctypes.CDLL("libc.so.6")
    libc.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
    libc.calloc.restype = ctypes.c_void_p
    libc.strdup.argtypes = [ctypes.c_char_p]
    libc.strdup.restype = ctypes.c_void_p
    libc.free.argtypes = [ctypes.c_void_p]
    libc.free.restype = None
    libc.explicit_bzero.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.explicit_bzero.restype = None
    return libc


def _conversation(current: str, password: str, libc):
    # The dedicated token module supplies these literal prompts. Do not guess
    # based on translated messages or send passwords to unexpected challenges.
    answers = {b"Current password: ": current.encode(), b"New password: ": password.encode(),
               b"Confirm new password: ": password.encode()}
    used = set()

    @Callback
    def converse(count, messages, responses, _data):
        responses[0] = ctypes.POINTER(Response)()
        if count < 1 or count > 32:
            return PAM_CONV_ERR
        allocation = libc.calloc(count, ctypes.sizeof(Response))
        if not allocation:
            return PAM_BUF_ERR
        result = ctypes.cast(allocation, ctypes.POINTER(Response))
        allocated = []
        success = False
        try:
            for index in range(count):
                message = messages[index].contents
                if message.style in {3, 4}:  # PAM_ERROR_MSG / PAM_TEXT_INFO
                    continue
                if message.style != 1 or message.text not in answers or message.text in used:
                    return PAM_CONV_ERR
                answer = answers[message.text]
                pointer = libc.strdup(answer)
                if not pointer:
                    return PAM_BUF_ERR
                allocated.append((pointer, len(answer)))
                result[index].text = pointer
                used.add(message.text)
            responses[0] = result
            success = True  # PAM now owns and frees the response allocations.
            return PAM_SUCCESS
        except Exception:
            return PAM_CONV_ERR
        finally:
            if not success:
                for pointer, length in allocated:
                    libc.explicit_bzero(pointer, length)
                    libc.free(pointer)
                libc.free(allocation)

    return converse


def _libpam():
    pam = ctypes.CDLL("libpam.so.0")
    pam.pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Conversation),
                              ctypes.POINTER(ctypes.c_void_p)]
    pam.pam_chauthtok.argtypes = [ctypes.c_void_p, ctypes.c_int]
    pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return pam


def change_password(username: str, current: str, password: str) -> None:
    pam = _libpam()
    callback = _conversation(current, password, _libc())
    conversation = Conversation(callback, None)
    handle = ctypes.c_void_p()
    result = pam.pam_start(SERVICE, username.encode(), ctypes.byref(conversation), ctypes.byref(handle))
    if result != PAM_SUCCESS:
        raise PamError(result)
    try:
        result = pam.pam_chauthtok(handle, 0)
        if result != PAM_SUCCESS:
            raise PamError(result)
    finally:
        pam.pam_end(handle, result)
