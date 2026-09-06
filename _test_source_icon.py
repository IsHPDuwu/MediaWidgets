"""播放源图标与名称测试：基类缓存/信号框架 + Windows 解析链。

- 基类：set_source_app_id 的去重、失败缓存、清空与信号；
- Windows：注册表 IconUri/DisplayName（png / file:/// 形态）、打包应用清单
  logo 与 ms-resource 显示名、进程 exe 内嵌图标与 FileDescription；
  安装了对应应用才测的项自动跳过。
"""
import base64
import os
import sys
import tempfile
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QCoreApplication, QIODevice
from PySide6.QtGui import QImage, QColor

app = QCoreApplication([])

from media_backend import MediaBackend
import smtc_backend as sb

fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def png_bytes(size=64, color="#C0392B"):
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def decode_data_url(url):
    if not url.startswith("data:image/png;base64,"):
        return None
    return QImage.fromData(base64.b64decode(url.split(",", 1)[1]))


# ---- 1. 基类框架 ----

class Dummy(MediaBackend):
    calls = []

    def _resolve_source(self, app_id):
        Dummy.calls.append(app_id)
        if app_id == "good.app":
            return ("Good App", "data:image/png;base64,AAAA")
        return ("", "")


d = Dummy()
names = []
icons = []
d.sourceNameChanged.connect(lambda: names.append(d.sourceName))
d.sourceIconChanged.connect(lambda: icons.append(d.sourceIcon))

d.set_source_app_id("good.app")
check("resolve on new app id", d.sourceName == "Good App"
      and d.sourceIcon.startswith("data:image/png;base64,"))
check("signals fired", len(names) == 1 and len(icons) == 1)

d.set_source_app_id("good.app")
check("same app id deduped", Dummy.calls == ["good.app"] and len(names) == 1)

d.set_source_app_id("bad.app")
check("failed resolve -> empty", d.sourceName == "" and d.sourceIcon == ""
      and Dummy.calls == ["good.app", "bad.app"])

d.set_source_app_id("good.app")
check("switch back hits cache", Dummy.calls.count("good.app") == 1 and d.sourceName != "")

d.set_source_app_id("")
check("empty app id clears", d.sourceName == "" and d.sourceIcon == "")

# ---- 2. 图标文件 / PE 提取 ----

# 冻结宿主（PyInstaller）不带未使用的标准库：smtc_backend 不得引入 xml.etree
# （v1.9.0 实机报 No module named 'xml.etree'，后端整体初始化失败）
if "xml.etree" not in sys.modules:
    check("no xml.etree dependency", "xml.etree" not in sys.modules)
else:
    print("[SKIP] xml.etree already loaded by environment")

# 清单解析：uap 命名空间前缀、属性提取、Id 字段
MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10">
  <Applications>
    <Application Id="Other" Executable="other.exe">
      <uap:VisualElements DisplayName="Other App" Square44x44Logo="Assets\\other.png"/>
    </Application>
    <Application Id="Main" Executable="app.exe">
      <uap:VisualElements DisplayName="ms-resource:AppName" Square44x44Logo="Assets\\logo.png"
                          Square150x150Logo="Assets\\tile.png"/>
    </Application>
  </Applications>
</Package>
"""
apps = sb._parse_manifest_applications(MANIFEST.encode("utf-8"))
check("manifest app count", len(apps) == 2, str([a["id"] for a in apps]))
main_app = next((a for a in apps if a["id"] == "Main"), None)
check("manifest attrs parsed", main_app is not None
      and main_app["ve"].get("DisplayName") == "ms-resource:AppName"
      and main_app["ve"].get("Square44x44Logo") == "Assets\\logo.png"
      and main_app["ve"].get("Square150x150Logo") == "Assets\\tile.png")
check("manifest empty attr safe", all("Logo" not in a["ve"] for a in apps))

tmp = Path(tempfile.mkdtemp())
png_path = tmp / "icon.png"
png_path.write_bytes(png_bytes(48, "#2E86C1"))

url = sb._win_icon_data_url_from_file(str(png_path))
img = decode_data_url(url)
check("png file -> data url", img is not None and not img.isNull() and img.width() == 48)

url = sb._win_icon_data_url_from_file("file:///" + str(png_path).replace("\\", "/"))
check("file:/// uri handled", url.startswith("data:image/png;base64,"))

url = sb._win_icon_data_url_from_file(str(tmp / "missing.png"))
check("missing file -> empty", url == "")

check("split index parsed", sb._split_icon_index(r"C:\x\e.exe,0") == (r"C:\x\e.exe", 0))
check("split index absent", sb._split_icon_index(r"C:\x\e.exe") == (r"C:\x\e.exe", 0))

pe = r"C:\Windows\System32\notepad.exe"
if not os.path.exists(pe):
    pe = r"C:\Windows\System32\shell32.dll"
url = sb._win_pe_icon_data_url(pe)
img = decode_data_url(url)
check("pe embedded icon", img is not None and not img.isNull() and img.width() > 0,
      f"{pe} -> {img.width() if img and not img.isNull() else 0}px")
url2 = sb._win_pe_icon_data_url(r"C:\Windows\System32\___no_such___.exe")
check("missing pe -> empty", url2 == "")

# ---- 3. 进程枚举 ----

exe_path = sb._win_process_exe_path(os.path.basename(sys.executable))
check("process exe path", exe_path and os.path.exists(exe_path)
      and exe_path.lower().endswith(".exe"), exe_path)
check("process not running -> empty", sb._win_process_exe_path("___no_such___.exe") == "")

# ---- 4. 注册表 IconUri（临时键，finally 清理） ----

TEST_KEY = r"SOFTWARE\Classes\AppUserModelId\MediaWidgetsTest.Icon"
try:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as k:
        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(png_path))
    url = sb._win_registry_icon("MediaWidgetsTest.Icon")
    check("registry IconUri png", url.startswith("data:image/png;base64,"))

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as k:
        file_uri = "file:///" + str(png_path).replace("\\", "/")
        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, file_uri)
    url = sb._win_registry_icon("MediaWidgetsTest.Icon")
    check("registry IconUri file:///", url.startswith("data:image/png;base64,"))
finally:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except OSError:
        pass

check("registry unknown aumid -> empty", sb._win_registry_icon("___no_such___.app") == "")

# ---- 5. 应用名称解析 ----

desc = sb._win_pe_file_description(r"C:\Windows\System32\notepad.exe")
check("pe file description", len(desc) >= 3, repr(desc))

name = sb._win_source_name("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic")
if name:
    check("packaged display name (ms-resource)", isinstance(name, str) and len(name) > 1, repr(name))
else:
    print("[SKIP] Microsoft.ZuneMusic not installed")

# ---- 6. 打包图标 / 端到端（装了对应应用才测） ----

url = sb._win_packaged_icon(
    sb._win_packaged_entries("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") or ("", "", []))
if url:
    img = decode_data_url(url)
    check("packaged manifest logo", img is not None and not img.isNull())
else:
    print("[SKIP] Microsoft.ZuneMusic not installed")

if sb._win_process_exe_path("cloudmusic.exe"):
    name = sb._win_source_name("cloudmusic.exe")
    url = sb._win_icon_from_aumid("cloudmusic.exe")
    img = decode_data_url(url)
    check("aumid end-to-end (cloudmusic.exe)",
          img is not None and not img.isNull() and name != "",
          repr(name))
else:
    print("[SKIP] cloudmusic.exe not running")

url = sb._win_icon_from_aumid("___no_such___.app")
check("aumid unresolvable -> empty", url == "")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
