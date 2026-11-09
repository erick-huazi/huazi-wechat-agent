#!/usr/bin/env python3
"""Resolve WeChat API credentials without storing secrets in the project."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from typing import Callable, Mapping, NamedTuple


CREDENTIAL_TARGET = "Codex/Huazi/WeChatOfficialAccount"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
WECHAT_CREDENTIAL_NAMES = ("WECHAT_APP_ID", "WECHAT_APP_SECRET")


class WeChatCredentials(NamedTuple):
    app_id: str
    app_secret: str
    source: str


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


def _advapi32():
    if os.name != "nt":
        return None
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredReadW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(PCREDENTIALW),
    ]
    library.CredReadW.restype = ctypes.c_int
    library.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), ctypes.c_uint32]
    library.CredWriteW.restype = ctypes.c_int
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def read_windows_credential(target: str = CREDENTIAL_TARGET) -> tuple[str, str] | None:
    library = _advapi32()
    if library is None:
        return None
    pointer = PCREDENTIALW()
    if not library.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            secret = blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            secret = blob.decode("utf-8").rstrip("\x00")
        return (credential.UserName or "", secret)
    finally:
        library.CredFree(pointer)


def write_windows_credential(
    app_id: str,
    app_secret: str,
    target: str = CREDENTIAL_TARGET,
) -> None:
    library = _advapi32()
    if library is None:
        raise RuntimeError("Windows Credential Manager is only available on Windows")
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    if not app_id or not app_secret:
        raise ValueError("app_id and app_secret are required")
    secret_bytes = app_secret.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(secret_bytes)).from_buffer_copy(secret_bytes)
    credential = CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(secret_bytes)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = app_id
    if not library.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise OSError(error, "CredWriteW failed")


def resolve_wechat_credentials(
    environment: Mapping[str, str] | None = None,
    credential_reader: Callable[[], tuple[str, str] | None] | None = None,
) -> WeChatCredentials | None:
    environment = environment if environment is not None else os.environ
    credential_reader = credential_reader or read_windows_credential
    app_id = environment.get("WECHAT_APP_ID", "").strip()
    app_secret = environment.get("WECHAT_APP_SECRET", "").strip()
    if app_id and app_secret:
        return WeChatCredentials(app_id, app_secret, "environment")
    stored = credential_reader()
    if stored and stored[0].strip() and stored[1].strip():
        return WeChatCredentials(stored[0].strip(), stored[1].strip(), "windows_credential_manager")
    return None


def hydrate_wechat_environment(environment: dict[str, str] | None = None) -> str:
    environment = environment if environment is not None else os.environ
    credentials = resolve_wechat_credentials(environment)
    if credentials is None:
        return "missing"
    environment.setdefault("WECHAT_APP_ID", credentials.app_id)
    environment.setdefault("WECHAT_APP_SECRET", credentials.app_secret)
    return credentials.source


def setup_windows_credentials(app_id: str) -> int:
    """Collect the secret in a masked local form, never via the terminal."""
    if os.name != "nt":
        raise RuntimeError("Credential setup requires Windows")
    import tkinter as tk
    from tkinter import messagebox, ttk

    window = tk.Tk()
    window.title("微信公众号 API 安全配置")
    window.geometry("640x380")
    window.resizable(False, False)
    panel = ttk.Frame(window, padding=24)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="公众号草稿 API 凭据", font=("Microsoft YaHei UI", 14)).pack(anchor="w")
    ttk.Label(
        panel,
        text="只保存到当前 Windows 用户的凭据管理器，不写入项目或日志。\n不会重置密钥、修改 IP 白名单或发布文章。",
        wraplength=540,
    ).pack(anchor="w", pady=(10, 16))
    ttk.Label(panel, text="AppID").pack(anchor="w")
    app_id_entry = ttk.Entry(panel, width=64)
    app_id_entry.insert(0, app_id)
    app_id_entry.pack(fill="x", pady=(4, 10))
    ttk.Label(panel, text="AppSecret（不是微信登录密码，请勿发送到聊天）").pack(anchor="w")
    secret_entry = ttk.Entry(panel, show="*", width=64)
    secret_entry.pack(fill="x", pady=(4, 14))
    saved = False

    def save() -> None:
        nonlocal saved
        identifier = app_id_entry.get().strip()
        secret = secret_entry.get().strip()
        if not identifier.startswith("wx") or not secret:
            messagebox.showwarning("尚未填写完整", "请填写 wx 开头的 AppID 和 AppSecret。", parent=window)
            return
        try:
            write_windows_credential(identifier, secret)
        except Exception:
            # Do not display exception text from credential-handling code.
            messagebox.showerror("未保存", "Windows 凭据写入失败，请检查当前用户权限。", parent=window)
            return
        finally:
            secret = ""
            secret_entry.delete(0, tk.END)
        saved = True
        messagebox.showinfo("已安全保存", "凭据已保存。接口权限及 IP 白名单仍需后续验证。", parent=window)
        window.destroy()

    buttons = ttk.Frame(panel)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="保存到 Windows 凭据管理器", command=save).pack(side="right")
    ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right", padx=12)
    window.bind("<Return>", lambda event: save())
    window.attributes("-topmost", True)
    window.after(1500, lambda: window.attributes("-topmost", False))
    secret_entry.focus_set()
    window.mainloop()
    return 0 if saved else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Manage secure WeChat API credentials")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    setup = sub.add_parser("setup", help="Open a masked local credential form")
    setup.add_argument("--app-id", default="")
    store = sub.add_parser("store")
    store.add_argument("--app-id", required=True)
    store.add_argument(
        "--secret-env",
        default="",
        help="Read the secret from this process environment variable instead of stdin.",
    )
    args = parser.parse_args()

    if args.command == "setup":
        return setup_windows_credentials(args.app_id)

    if args.command == "status":
        credentials = resolve_wechat_credentials()
        print(f"configured={'true' if credentials else 'false'}")
        print(f"source={credentials.source if credentials else 'missing'}")
        return 0 if credentials else 1

    secret = (
        os.environ.get(args.secret_env, "").strip()
        if args.secret_env
        else sys.stdin.readline().strip()
    )
    if not secret:
        raise SystemExit("secret is required")
    write_windows_credential(args.app_id, secret)
    print("stored=true")
    print(f"target={CREDENTIAL_TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
