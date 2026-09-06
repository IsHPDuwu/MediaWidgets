"""
smtc_backend.py
SMTC Backend（Windows）
负责从 Windows SMTC (System Media Transport Controls) 获取当前媒体信息。

更新策略（沿用基类）：
- 订阅 SMTC 事件（换歌 / 播放暂停 / 进度跳变 / 会话切换）实现即时更新；
- 工作线程 asyncio 事件循环里拉取，经排队信号在主线程应用；
- 主线程 250ms 插值刷新播放进度；5s 兜底全量同步。
"""

import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import os
import threading
import winreg

from loguru import logger
from PySide6.QtCore import QXmlStreamReader, QUrl
from PySide6.QtGui import QImage

from media_backend import MediaBackend

# 包安装仓库：子键为 <包名>_<版本>[_架构]（不含发布者哈希），Path 值指向安装目录
_PKG_REPO = (r"SOFTWARE\Classes\Local Settings\Software\Microsoft\Windows"
             r"\CurrentVersion\AppModel\PackageRepository\Packages")


# ---- 播放源图标解析（Windows）----
# 解析链：AUMID 注册表 IconUri → 打包应用清单 logo → 进程 exe 内嵌图标。
# 只用 ctypes / winreg / Qt（纯光栅），不触碰 QApplication，可在 SMTC 工作线程调用。


def _icon_data_url(img):
    return MediaBackend._icon_data_url(img)


def _win_resolve_indirect(s):
    """@{...?ms-resource:...} 之类的间接字符串经 SHLoadIndirectString 解析。"""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.shlwapi.SHLoadIndirectString(s, buf, 1024, None) == 0:
            return buf.value
    except Exception:
        pass
    return ""


def _win_normalize_icon_uri(raw):
    """注册表 IconUri / IconResource 的几种形态（间接串、file:///、含环境变量）转本地路径。"""
    s = str(raw).strip()
    if s.startswith("@"):
        s = _win_resolve_indirect(s)
    if not s:
        return ""
    if s.startswith("file:"):
        s = QUrl(s).toLocalFile()
    return os.path.expandvars(s)


def _split_icon_index(path):
    """'X\\foo.exe,0' → ('X\\foo.exe', 0)；无合法索引时原样返回。"""
    stem, _, tail = path.rpartition(",")
    if stem and tail.strip().isdigit():
        return stem, int(tail)
    return path, 0


def _win_icon_data_url_from_file(path, index=0):
    """图标文件（png/ico/…）或 PE（exe/dll）→ PNG data URL，失败返回空串。"""
    if not path:
        return ""
    try:
        if path.startswith("file:"):
            path = QUrl(path).toLocalFile()
        low = path.lower()
        if low.endswith((".exe", ".dll")):
            return _win_pe_icon_data_url(path, index)
        return _icon_data_url(QImage(path))
    except Exception:
        return ""


def _win_registry_icon(app_id):
    """HKCU/HKLM Software\\Classes\\AppUserModelId\\<AUMID> 的 IconUri / IconResource。"""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Classes\AppUserModelId" + "\\" + app_id)
        except OSError:
            continue
        with key:
            for value_name in ("IconUri", "IconResource"):
                try:
                    raw, _ = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if not isinstance(raw, str) or not raw:
                    continue
                path = _win_normalize_icon_uri(raw)
                stem, index = _split_icon_index(path)
                url = _win_icon_data_url_from_file(stem, index)
                if url:
                    return url
    return ""


def _win_registry_display_name(app_id):
    """注册表 AppUserModelId 记录的 DisplayName（部分应用注册通知时写入）。"""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Classes\AppUserModelId" + "\\" + app_id)
        except OSError:
            continue
        with key:
            try:
                raw, _ = winreg.QueryValueEx(key, "DisplayName")
            except OSError:
                continue
            if not isinstance(raw, str) or not raw:
                continue
            if raw.startswith("@"):
                raw = _win_resolve_indirect(raw)
            if raw and not raw.startswith("@"):
                return raw.strip()
    return ""


def _win_package_candidates(name):
    """包仓库里 <name> 开头的已安装包，按版本新 → 旧返回 (包全名, 安装目录)。

    跳过语言 split 包与 ~ bundle 记录（主包才有含 Application 的清单）。
    """
    prefix = name + "_"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PKG_REPO) as root:
            count, _, _ = winreg.QueryInfoKey(root)
            matches = []
            for i in range(count):
                full = winreg.EnumKey(root, i)
                if full.startswith(prefix) and "_split." not in full and "_~_" not in full:
                    matches.append(full)
    except OSError:
        return []

    def version(key):
        parts = []
        for p in key[len(prefix):].split("_")[0].split("."):
            try:
                parts.append((0, int(p)))
            except ValueError:
                parts.append((1, p))
        return parts

    result = []
    for full in sorted(matches, key=version, reverse=True):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PKG_REPO + "\\" + full) as sk:
                pkg_path = winreg.QueryValueEx(sk, "Path")[0]
        except OSError:
            continue
        result.append((full, pkg_path))
    return result


def _win_pick_logo_variant(directory, stem, ext):
    """同名 logo 变体里挑分辨率最高、非单色（unplated/contrast）的一张。"""
    priorities = (
        ("targetsize-256", 0), ("targetsize-96", 1), ("targetsize-48", 2),
        ("scale-200", 3), ("scale-100", 4),
    )
    ranked = []
    try:
        entries = os.listdir(directory)
    except OSError:
        return ""
    for f in entries:
        low = f.lower()
        if not low.startswith(stem.lower()) or not low.endswith(ext.lower()):
            continue
        if "unplated" in low or "contrast" in low:
            continue
        for needle, prio in priorities:
            if needle in low:
                ranked.append((prio, f))
                break
        else:
            ranked.append((5, f))
    if not ranked:
        return ""
    ranked.sort()
    return os.path.join(directory, ranked[0][1])


def _parse_manifest_applications(xml_bytes):
    """AppxManifest → [{id, ve}]（Application 列表，ve 为其 VisualElements 属性）。

    用 Qt 流式解析器而非标准库 xml：宿主以 PyInstaller 冻结发行，未用到的
    标准库模块不会打进包里（v1.9.0 实机报 No module named 'xml.etree'）。
    """
    reader = QXmlStreamReader(xml_bytes.decode("utf-8", "replace"))
    apps = []
    current = None
    while not reader.atEnd() and not reader.hasError():
        if reader.isStartElement():
            name = str(reader.name())
            if name == "Application":
                current = {"id": str(reader.attributes().value("Id") or ""), "ve": {}}
                apps.append(current)
            elif name == "VisualElements" and current is not None:
                for attr in ("DisplayName", "Square44x44Logo", "Square150x150Logo", "Logo"):
                    value = reader.attributes().value(attr)
                    if value:
                        current["ve"][attr] = str(value)
        elif reader.isEndElement() and str(reader.name()) == "Application":
            current = None
        reader.readNext()
    return apps


def _win_manifest_logo(pkg_path, apps, entry_id):
    """AppxManifest 里目标 Application 的 logo 绝对路径；Id 对不上时取第一个带 logo 的。"""
    # 精确匹配排最前（稳定排序），兜底其余应用
    ordered = sorted(apps, key=lambda app: app["id"] != entry_id)
    for app in ordered:
        for attr in ("Square44x44Logo", "Square150x150Logo", "Logo"):
            rel = app["ve"].get(attr)
            if not rel:
                continue
            p = os.path.join(pkg_path, os.path.normpath(rel))
            stem, ext = os.path.splitext(p)
            chosen = _win_pick_logo_variant(os.path.dirname(p), os.path.basename(stem), ext)
            if chosen:
                return chosen
    return ""


def _win_packaged_entries(app_id):
    """AUMID（形如 Family!AppId）→ (family, app_id, [(包全名, 目录), …])；非打包应用返回 None。"""
    if "!" not in app_id:
        return None
    family, entry_id = app_id.split("!", 1)
    name = family.rsplit("_", 1)[0]  # 包名不含下划线
    return family, entry_id, _win_package_candidates(name)


def _win_packaged_icon(entries):
    """打包应用：AppxManifest → 目标应用 logo → 图标 data URL。"""
    family, entry_id, candidates = entries
    for full, pkg_path in candidates:
        try:
            with open(os.path.join(pkg_path, "AppxManifest.xml"), "rb") as f:
                apps = _parse_manifest_applications(f.read())
        except OSError:
            continue
        logo = _win_manifest_logo(pkg_path, apps, entry_id)
        if logo:
            stem, index = _split_icon_index(logo)
            url = _win_icon_data_url_from_file(stem, index)
            if url:
                return url
    return ""


def _win_packaged_display_name(entries):
    """打包应用：清单 DisplayName；ms-resource 资源经 SHLoadIndirectString 按系统语言解析。"""
    family, entry_id, candidates = entries
    for full, pkg_path in candidates:
        try:
            with open(os.path.join(pkg_path, "AppxManifest.xml"), "rb") as f:
                apps = _parse_manifest_applications(f.read())
        except OSError:
            continue
        # 精确匹配排最前（稳定排序）；其清单名解析不了时再试其余应用
        ordered = sorted(apps, key=lambda app: app["id"] != entry_id)
        for app in ordered:
            display = app["ve"].get("DisplayName")
            if not display:
                continue
            if not display.startswith("ms-resource:"):
                return display
            if "://" in display:
                uri = display
            else:
                uri = f"ms-resource://{family}/resources/{display.split(':', 1)[1]}"
            resolved = _win_resolve_indirect(f"@{{{full}?{uri}}}")
            if resolved and not resolved.startswith("@{"):
                return resolved
    return ""


def _win_process_exe_path(name):
    """按进程名找可执行文件完整路径（多实例取第一个查询成功的）。"""
    target = name.lower()
    stem = target[:-4] if target.endswith(".exe") else target
    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
        ]

    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wt.HANDLE(-1):
        return ""
    pids = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            exe = pe.szExeFile.lower()
            if exe == target or (exe[:-4] if exe.endswith(".exe") else exe) == stem:
                pids.append(pe.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)

    k32.OpenProcess.restype = wt.HANDLE
    for pid in pids:
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)
    return ""


def _win_pe_icon_data_url(path, index=0):
    """PrivateExtractIcons 提取 exe/dll 内嵌图标 → PNG data URL（失败返回空串）。"""
    u32 = ctypes.windll.user32
    g32 = ctypes.windll.gdi32

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                    ("hbmMask", wt.HANDLE), ("hbmColor", wt.HANDLE)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wt.LONG), ("bmWidth", wt.LONG), ("bmHeight", wt.LONG),
                    ("bmWidthBytes", wt.LONG), ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BMIHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    u32.PrivateExtractIconsW.argtypes = [
        wt.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(wt.HICON), ctypes.POINTER(wt.INT), ctypes.c_int, wt.UINT,
    ]
    u32.GetIconInfo.argtypes = [wt.HICON, ctypes.POINTER(ICONINFO)]
    g32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
    g32.GetDIBits.argtypes = [wt.HANDLE, wt.HANDLE, wt.UINT, wt.UINT,
                              ctypes.c_void_p, ctypes.c_void_p, wt.UINT]

    hicon = wt.HICON()
    try:
        if u32.PrivateExtractIconsW(path, index, 64, 64,
                                    ctypes.byref(hicon), None, 1, 0) != 1:
            return ""
        ii = ICONINFO()
        if not u32.GetIconInfo(hicon, ctypes.byref(ii)) or not ii.hbmColor:
            return ""
        try:
            bmp = BITMAP()
            if g32.GetObjectW(ii.hbmColor, ctypes.sizeof(bmp), ctypes.byref(bmp)) == 0:
                return ""
            w, h = bmp.bmWidth, bmp.bmHeight
            if w <= 0 or h <= 0 or bmp.bmBitsPixel != 32:
                return ""  # 旧式无 alpha 图标：宁缺毋滥，角标直接不显示
            bmi = BMIHEADER()
            bmi.biSize = ctypes.sizeof(BMIHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # 负值 = 自上而下
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            hdc = g32.CreateCompatibleDC(None)
            got = 0
            if hdc:
                got = g32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bmi), 0)
                g32.DeleteDC(hdc)
            if got != h:
                return ""
            data = bytes(buf.raw)
            # 无 alpha 通道的旧图标整图全透明，按不透明处理
            fmt = (QImage.Format.Format_ARGB32_Premultiplied
                   if any(data[i] for i in range(3, len(data), 4))
                   else QImage.Format.Format_RGB32)
            img = QImage(data, w, h, w * 4, fmt)
            if img.isNull():
                return ""
            img = img.copy()  # 脱离临时缓冲
            return _icon_data_url(img)
        finally:
            if ii.hbmMask:
                g32.DeleteObject(wt.HANDLE(ii.hbmMask))
            if ii.hbmColor:
                g32.DeleteObject(wt.HANDLE(ii.hbmColor))
    except Exception:
        return ""
    finally:
        if hicon:
            u32.DestroyIcon(hicon)


def _win_pe_file_description(path):
    """exe 版本信息里的 FileDescription（随程序内嵌语言，如「记事本」「NetEase Cloud Music」）。"""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, data):
            return ""
        val = ctypes.c_void_p()
        vlen = wt.UINT()
        if not ver.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                 ctypes.byref(val), ctypes.byref(vlen)):
            return ""
        count = vlen.value // 4
        table = ctypes.cast(val, ctypes.POINTER(wt.DWORD * max(count, 1))).contents
        # 简中 / 英文资源优先
        langs = sorted(
            ((v & 0xFFFF, (v >> 16) & 0xFFFF) for v in table[:count]),
            key=lambda t: 0 if t[0] in (0x0804, 0x0409) else 1,
        )
        for lang, codepage in langs:
            buf = ctypes.c_void_p()
            blen = wt.UINT()
            sub = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            if ver.VerQueryValueW(data, sub, ctypes.byref(buf), ctypes.byref(blen)) and blen.value:
                # puLen 为字符数（含结尾空字符），非字节数
                text = ctypes.wstring_at(buf, blen.value - 1)
                if text.strip():
                    return text.strip()
    except Exception:
        pass
    return ""


def _win_source_name(app_id):
    """播放源显示名：注册表 DisplayName → 打包清单 ms-resource → exe FileDescription → 文件名。"""
    entries = _win_packaged_entries(app_id)
    name = _win_registry_display_name(app_id)
    if not name and entries:
        name = _win_packaged_display_name(entries)
    if not name:
        exe = _win_process_exe_path(os.path.basename(app_id.replace("\\", "/")))
        if exe:
            name = (_win_pe_file_description(exe)
                    or os.path.splitext(os.path.basename(exe))[0])
    return name or ""


def _win_icon_from_aumid(app_id):
    url = _win_registry_icon(app_id)
    if url:
        return url
    entries = _win_packaged_entries(app_id)
    if entries:
        url = _win_packaged_icon(entries)
        if url:
            return url
    exe = _win_process_exe_path(os.path.basename(app_id.replace("\\", "/")))
    if exe:
        return _win_pe_icon_data_url(exe)
    return ""


class SmtcBackend(MediaBackend):
    """Windows SMTC 媒体后端。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager_cls = None
        self._loop = None
        self._thread = None
        self._manager = None
        self._manager_token = None
        self._session = None
        self._session_id = None
        self._session_tokens = []

    # ---- 启动 ----

    def start(self):
        """启动 SMTC 事件订阅与进度插值（在主线程调用）。"""
        if self._tick_timer is not None:
            return

        logger.info("SMTC: start() called, importing winrt...")
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )
            self._manager_cls = GlobalSystemMediaTransportControlsSessionManager
        except Exception as e:
            logger.error(f"SMTC: winrt import failed: {e}")
            return

        super().start()  # 进度插值定时器 + _start_source()

    def _start_source(self):
        # asyncio 事件循环线程
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._bootstrap(), self._loop)

    def _resync(self):
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._light_sync(), self._loop)

    def _resolve_source(self, app_id):
        """播放源 AUMID → (应用名, 图标 data URL)（SMTC 工作线程调用，结果经信号回主线程）。"""
        return _win_source_name(app_id), _win_icon_from_aumid(app_id)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ---- 事件订阅（工作线程 / WinRT 线程）----

    async def _bootstrap(self):
        try:
            manager = await self._manager_cls.request_async()
            self._manager = manager
            try:
                # 裸 Python 可调用可直接作为 WinRT 委托传入
                self._manager_token = manager.add_sessions_changed(self._on_sessions_changed)
            except Exception as e:
                logger.warning(f"SMTC: sessions_changed subscribe failed: {e}")
            await self._sync_state()
            logger.info("SMTC: event-driven mode ready")
        except Exception as e:
            logger.error(f"SMTC: bootstrap failed: {e}")

    def _on_sessions_changed(self, sender, args):
        """会话集合变化（应用开/关）：重建订阅并全量拉取。"""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._sync_state(), self._loop)

    def _on_any_session_event(self, sender, args):
        """任一会话的属性/播放/时间线事件：刷新当前会话并拉取。

        订阅全部会话而非仅当前会话——当前会话的切换（如另一应用开始播放）
        不一定伴随 sessions_changed 事件。
        """
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._light_sync(), self._loop)

    async def _sync_state(self):
        self._resubscribe_sessions()
        await self._fetch()

    async def _light_sync(self):
        self._update_current_session()
        await self._fetch()

    def _resubscribe_sessions(self):
        """退订全部旧会话事件，重新订阅当前所有会话。"""
        self._unsubscribe_sessions()
        try:
            sessions = list(self._manager.get_sessions())
        except Exception as e:
            logger.warning(f"SMTC: get_sessions failed: {e}")
            sessions = []
        for session in sessions:
            self._subscribe_session(session)
        self._update_current_session()

    def _subscribe_session(self, session):
        for name in (
            "media_properties_changed",
            "playback_info_changed",
            "timeline_properties_changed",
        ):
            try:
                token = getattr(session, f"add_{name}")(self._on_any_session_event)
                self._session_tokens.append((session, f"remove_{name}", token))
            except Exception as e:
                logger.warning(f"SMTC: subscribe {name} failed: {e}")

    def _unsubscribe_sessions(self):
        for session, remover, token in self._session_tokens:
            try:
                getattr(session, remover)(token)
            except Exception:
                pass
        self._session_tokens = []

    def _update_current_session(self):
        """刷新当前会话引用；当前会话可能在会话集合不变时切换。"""
        session = self._manager.get_current_session() if self._manager else None
        new_id = None
        if session is not None:
            try:
                new_id = session.source_app_user_model_id
            except Exception:
                new_id = None
        if new_id == self._session_id:
            return
        logger.info(f"SMTC: current session -> {new_id}")
        self._session = session
        self._session_id = new_id

    # ---- 数据拉取（工作线程）----

    async def _fetch(self):
        try:
            session = self._session
            if session is None:
                self.set_source_app_id("")
                self._mediaUpdated.emit("", "", "", 0, 0, *self._last_palette)
                self._playbackUpdated.emit(0, 1.0)
                self._timelineUpdated.emit(0, 0)
                return

            self.set_source_app_id(self._session_id or "")
            props = await session.try_get_media_properties_async()
            new_title = props.title or ""
            new_artist = props.artist or ""
            new_art = await self._load_art(props)

            position_ms, duration_ms = self._read_timeline(session)
            status, rate = self._read_playback(session)

            accent1, accent2 = self._last_palette
            if new_art:
                accent1, accent2 = self._extract_palette(new_art)

            logger.debug(
                f"SMTC: fetch title={new_title!r} pos={position_ms} dur={duration_ms} "
                f"status={status} art={bool(new_art)}"
            )
            self._mediaUpdated.emit(new_title, new_artist, new_art, position_ms, duration_ms, accent1, accent2)
            if status is not None:
                self._playbackUpdated.emit(status, rate if rate else 1.0)
            self._timelineUpdated.emit(position_ms, duration_ms)
        except Exception as e:
            logger.error(f"SMTC: fetch failed: {e}")

    def _read_timeline(self, session):
        """同步读取时间线，返回 (position_ms, duration_ms)；失败时保留旧值。"""
        try:
            ti = session.get_timeline_properties()
            pos = getattr(ti, "position", None)
            end = getattr(ti, "end_time", None)
            position_ms = int(pos.total_seconds() * 1000) if pos is not None else self._position_ms
            duration_ms = int(end.total_seconds() * 1000) if end is not None else self._duration_ms
            return position_ms, duration_ms
        except Exception as e:
            logger.debug(f"SMTC: get timeline failed: {e}")
            return self._position_ms, self._duration_ms

    @staticmethod
    def _read_playback(session):
        """同步读取播放状态，返回 (status, rate)；失败返回 (None, None)。"""
        try:
            pb = session.get_playback_info()
            status = int(pb.playback_status)
            rate = getattr(pb, "playback_rate", None)
            return status, (float(rate) if rate else None)
        except Exception as e:
            logger.debug(f"SMTC: get playback info failed: {e}")
            return None, None

    async def _load_art(self, props) -> str:
        """读取 SMTC 缩略图并转为圆角 PNG data URL（失败/无图返回空字符串）。"""
        stream = None
        try:
            thumbnail = getattr(props, "thumbnail", None)
            if thumbnail is None:
                return ""
            stream = await thumbnail.open_read_async()
            size = stream.size
            if size <= 0 or size > 20 * 1024 * 1024:
                return ""

            from winrt.windows.storage.streams import DataReader
            reader = DataReader(stream)
            loaded = await reader.load_async(size)
            if loaded <= 0:
                return ""
            raw = bytearray(loaded)
            try:
                # winrt-runtime 3.x：read_bytes 接收可写缓冲区并原地填充，无返回值
                # （传 int 会报 "a bytes-like object is required, not 'int'"）
                reader.read_bytes(raw)
            except TypeError:
                # 旧版 winsdk / pywinrt 2.x：read_bytes(count) -> bytes
                raw = reader.read_bytes(loaded)

            return self.process_art_bytes(bytes(raw))
        except Exception as e:
            logger.debug(f"SMTC: load album art failed: {e}")
            return ""
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
