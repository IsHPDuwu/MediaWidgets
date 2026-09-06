"""
Media Widgets
一个显示系统媒体信息与逐字歌词的 Class Widgets 插件。
Windows 走 SMTC，Linux 走 MPRIS；歌词支持 QQ音乐（QRC 逐字）、
酷狗（KRC 逐字）、网易云（LRC 行级），在自有的歌词小组件中卡拉OK渲染。
"""

import sys
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QCoreApplication, QTranslator
from ClassWidgets.SDK import CW2Plugin, PluginAPI

from plugin_config import MediaWidgetsConfig

_I18N_DIR = Path(__file__).resolve().parent / "i18n"


def _catalog_for_language(lang: str) -> str:
    """CW2 界面语言（QLocale.name() 格式）→ 插件翻译目录的目录名。

    翻译源语言为简体中文：zh_CN 直接用源文，繁中（含 zh_HK / 文言）走 zh_TW，
    日英各走自己的目录；CW2 未提供的其余语言回落英文（与宿主行为一致）。
    """
    l = (lang or "").replace("-", "_").strip().lower()
    if l == "lzh":
        return "zh_TW"
    if l.startswith("zh"):
        if any(k in l for k in ("hk", "tw", "mo")):
            return "zh_TW"
        return "zh_CN"
    if l.startswith("ja"):
        return "ja_JP"
    return "en_US"


class Plugin(CW2Plugin):
    def __init__(self, api: PluginAPI):
        super().__init__(api)
        # 请在此导入第三方库 / Import third-party libraries here
        self._backend = None
        self._lyrics_backend = None
        self._config = MediaWidgetsConfig()
        self._translator = None
        self._catalog = None

    def on_load(self):
        super().on_load()
        logger.info("Media Widgets: on_load() called")

        # 跟随 CW2 界面语言加载插件翻译（qsTr 源语言为简体中文）
        self._setup_translator()

        # 注册插件配置模型：把默认值落进 configs.plugins.configs[pid]，
        # QML 设置页由此读到初始状态；运行时改动经 Configs.setPlugin 写回同一字典
        try:
            if self.pid:
                self.api.config.register_plugin_model(self.pid, self._config)
        except Exception as e:
            logger.warning(f"Media Widgets: register config model failed: {e}")

        # 注册同名设置页：挂在 CW2 设置 → 插件 → Media Widgets
        try:
            self.api.ui.register_settings_page(
                qml_path="qml/MediaWidgetsSettings.qml",
                title="Media Widgets",
                icon="ic_fluent_music_note_2_20_regular",
            )
        except Exception as e:
            logger.warning(f"Media Widgets: register settings page failed: {e}")

        # 创建 backend 对象（延迟导入，数据源不可用时也不阻止 widget 注册）
        try:
            if sys.platform == "win32":
                from smtc_backend import SmtcBackend
                self._backend = SmtcBackend()
                logger.info("Media Widgets: SmtcBackend created")
            else:
                from mpris_backend import MprisBackend
                self._backend = MprisBackend()
                logger.info("Media Widgets: MprisBackend created")
        except Exception as e:
            logger.error(f"Media Widgets: backend init failed: {e}")
            self._backend = None

        # 注册 widget（无论 backend 是否成功都要注册）
        self.api.widgets.register(
            widget_id="com.seiraiharaguchi.mediawidgets.widget",
            name="Media Widget",
            qml_path="qml/MediaWidget.qml",
            backend_obj=self._backend,
        )
        logger.info("Media Widgets: widget registered")

        if self._backend is not None:
            # 歌词组件后端：换歌抓取（磁盘缓存优先）→ 逐字数据/副行/进度节拍
            try:
                from lyrics_backend import LyricsBackend
                self._lyrics_backend = LyricsBackend(
                    self._backend, self._live_config_getter())
                self.api.widgets.register(
                    widget_id="com.seiraiharaguchi.mediawidgets.lyrics",
                    name="Lyrics Widget",
                    qml_path="qml/LyricsWidget.qml",
                    backend_obj=self._lyrics_backend,
                )
                logger.info("Media Widgets: lyrics widget registered")
            except Exception as e:
                logger.error(f"Media Widgets: lyrics widget init failed: {e}")
                self._lyrics_backend = None

            # 把媒体后端暴露给「设置 → 插件 → Media Widgets」页面，
            # 使设置页能实时显示正在播放信息
            if self.pid:
                try:
                    from src.core.plugin.bridge import PluginBackendBridge
                    PluginBackendBridge.register_backend(self.pid, self._backend)
                except Exception as e:
                    logger.debug(f"Media Widgets: expose backend to settings page failed: {e}")

            # 延迟启动媒体与歌词后端（确保 Qt 事件循环已启动）
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(1000, self._backend.start)
                if self._lyrics_backend is not None:
                    QTimer.singleShot(1200, self._lyrics_backend.start)
                logger.info("Media Widgets: scheduled backend start in 1s")
            except Exception as e:
                logger.error(f"Media Widgets: backend start failed: {e}")
        else:
            logger.warning("Media Widgets: backend is None, skipping start")

    def _setup_translator(self):
        """按 CW2 界面语言安装插件 QM 翻译，宿主切换语言时跟随更新。

        CW2 任何配置变更都会广播 ConfigManager.configChanged，这里做廉价比对，
        仅在 locale.language 对应目录变化时换装翻译器；QML 的 qsTr 绑定会随
        LanguageChange 事件自动重译。
        """
        try:
            self.api.globalconfig.configs.configChanged.connect(self._refresh_translator)
        except Exception as e:
            logger.debug(f"Media Widgets: subscribe language change failed: {e}")
        self._refresh_translator()

    def _refresh_translator(self):
        try:
            lang = self.api.globalconfig.configs.locale.language
        except Exception:
            lang = ""
        catalog = _catalog_for_language(lang)
        if catalog == self._catalog:
            return
        app = QCoreApplication.instance()
        if app is None:
            return
        if self._translator is not None:
            try:
                app.removeTranslator(self._translator)
            except Exception:
                pass
            self._translator = None
        self._catalog = catalog
        if catalog == "zh_CN":
            return  # 源语言即简体中文
        qm = _I18N_DIR / f"MediaWidgets_{catalog}.qm"
        translator = QTranslator(app)
        if not translator.load(str(qm)):
            logger.warning(f"Media Widgets: translation catalog not loaded: {qm}")
            return
        app.installTranslator(translator)
        self._translator = translator
        logger.info(f"Media Widgets: language -> {catalog} ({lang})")

    def _live_config_getter(self):
        """返回实时读取本插件配置的函数。

        QML 设置页的改动经 Configs.setPlugin 写进 configs.plugins.configs[pid]
        （CW2 不会把字典变更同步回注册的模型实例），所以 Python 侧每次都从
        配置管理器现读，保证改动立即生效。读取失败返回 None，由调用方回退默认值。
        """
        pid = self.pid
        try:
            configs = self.api.globalconfig.configs
        except Exception as e:
            logger.warning(f"Media Widgets: access config manager failed: {e}")
            return lambda key: None

        def getter(key):
            try:
                section = configs.plugins.configs.get(pid)
                if not isinstance(section, dict):
                    return None
                return section.get(key)
            except Exception:
                return None

        return getter

    def on_unload(self):
        logger.info("Media Widgets: on_unload() called")
        if self._translator is not None:
            try:
                app = QCoreApplication.instance()
                if app is not None:
                    app.removeTranslator(self._translator)
            except Exception:
                pass
            self._translator = None
        if self._lyrics_backend is not None:
            try:
                self._lyrics_backend.stop()
            except Exception:
                pass
