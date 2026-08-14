"""本地外部可执行解析。

Windows 上 ``CreateProcess`` 会优先命中 ``C:\\Windows\\System32`` 的 WSL
``bash.exe`` / bsdtar ``tar.exe``（而非 PATH 里 Git Bash 自带的 GNU 版本），
生产代码凡需本地 tar/bash 一律经本模块解析，避免 WSL/bsdtar 行为差异。
"""
from __future__ import annotations

import functools
import os
import shutil


@functools.lru_cache(maxsize=None)
def gnu_tar() -> str:
    """解析 GNU tar 可执行路径（Windows 优先 Git for Windows 自带的 usr/bin/tar）。"""
    if os.name == "nt":
        cand = os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "Git", "usr", "bin", "tar.exe",
        )
        if os.path.isfile(cand):
            return cand
    return shutil.which("tar") or "tar"


def tar_path(p) -> str:
    """把本地路径转成 GNU tar(MSYS) 能识别的形式：Windows ``C:\\a\\b`` → ``/c/a/b``。

    Git for Windows 的 tar 不把反斜杠 Windows 路径当目录（会按字面文件名找不到），
    生产代码给本地 tar 传路径一律经本函数；非 Windows 原样返回。
    """
    s = str(p)
    if os.name == "nt":
        if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
            s = "/" + s[0].lower() + s[2:]
        return s.replace("\\", "/")
    return s
