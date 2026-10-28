"""API 密钥的本机加密存储。

Windows 下使用 DPAPI（CryptProtectData），密文与当前用户账户绑定，
即便配置文件被复制到其他机器也无法解密。非 Windows 平台降级为
Base64 占位存储并在调用方提示风险。
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

from ..atomic import write_text as atomic_write_text

IS_WINDOWS = sys.platform == "win32"


class KeyStoreError(RuntimeError):
    pass


if IS_WINDOWS:

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    _kernel32.LocalFree.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [wintypes.HANDLE]

    def _raw_protect(data: bytes) -> bytes:
        # buffer 必须在本作用域内存活，否则 pbData 会悬空
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        if not _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise KeyStoreError(f"CryptProtectData 失败 (错误码 {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(blob_out.pbData)

    def _raw_unprotect(data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raise KeyStoreError(f"CryptUnprotectData 失败 (错误码 {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(blob_out.pbData)

    def encrypt_text(plain: str) -> str:
        return base64.b64encode(_raw_protect(plain.encode("utf-8"))).decode("ascii")

    def decrypt_text(token: str) -> str:
        return _raw_unprotect(base64.b64decode(token.encode("ascii"))).decode("utf-8")

else:
    # 非 Windows 平台无 DPAPI，明确降级而非静默明文
    def encrypt_text(plain: str) -> str:
        return "PLAIN:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")

    def decrypt_text(token: str) -> str:
        if token.startswith("PLAIN:"):
            return base64.b64decode(token[6:].encode("ascii")).decode("utf-8")
        raise KeyStoreError("当前平台不支持 DPAPI 密文解密")


def save_api_key(path, api_key: str) -> None:
    """保存密钥。清空即删除文件。

    写入走 `atomic.write_text`（同目录 .tmp → os.replace），与 `db.backup_to` 同一
    范式：直接 write_text 到目标路径时，中途失败会留下一段**半截密文**，而
    load_api_key 解不开就静默返回空串——用户看到「未填写密钥」，完全不知道
    是被写坏的，还得重填一次。密钥文件很小，多这一步几乎不花时间。
    """
    if not api_key:
        path.unlink(missing_ok=True)
        return
    atomic_write_text(path, encrypt_text(api_key), encoding="ascii")


def load_api_key(path) -> str:
    if not path.exists():
        return ""
    try:
        return decrypt_text(path.read_text(encoding="ascii").strip())
    except (KeyStoreError, ValueError, OSError):
        return ""
