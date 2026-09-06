import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import RinUI

// Media Widgets 插件同名设置页（CW2 设置 → 插件 → Media Widgets）
// - main.py 在 on_load 时经 api.ui.register_settings_page 注册本页；
// - 在播信息来自 main.py 注册进 PluginBackendBridge 的媒体后端（与桌面小组件同源）；
// - 开关经 Configs.setPlugin 写入 configs.plugins.configs[pid]，
//   Python 侧（lyrics_pusher）从同一路径实时读取，改动立即生效。
FluentPage {
    id: root
    horizontalPadding: 0
    wrapperWidth: width - 42 * 2
    spacing: 4
    title: qsTr("Media Widgets")

    // 插件 id 固定：RinUI 导航项点击不透传 properties，页面自持 id
    property string pluginId: "com.seiraiharaguchi.mediawidgets"
    property var backend: typeof PluginBackendBridge !== "undefined"
                          ? PluginBackendBridge.get_backend(pluginId) : null
    property bool hasMedia: root.backend && root.backend.title !== ""

    function config(key, fallback) {
        var cfg = Configs.data.plugins && Configs.data.plugins.configs
        if (!cfg || !cfg[root.pluginId]) return fallback
        var v = cfg[root.pluginId][key]
        return v === undefined ? fallback : v
    }

    // ---------- 正在播放 ----------

    Text {
        Layout.fillWidth: true
        Layout.topMargin: 8
        typography: Typography.BodyStrong
        text: qsTr("正在播放")
    }

    Frame {
        id: nowPlayingCard
        Layout.fillWidth: true
        Layout.topMargin: 4
        hoverable: false
        leftPadding: 16
        rightPadding: 16
        topPadding: 16
        bottomPadding: 16
        // 卡内有两个子项（内容列 + 右上角覆盖行），Pane 无法自动推算隐式大小，
        // 按官方文档显式绑定内容高度，否则 Frame 塌缩成一条、内容被 clip 裁掉
        contentHeight: mediaColumn.implicitHeight

        ColumnLayout {
            id: mediaColumn
            anchors.left: parent.left
            anchors.right: parent.right
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                spacing: 16

                // 封面：后端输出的 PNG 已烘焙圆角（64px 显示 ≈ 14px 半径）
                Item {
                    Layout.preferredWidth: 64
                    Layout.preferredHeight: 64

                    Rectangle {
                        anchors.fill: parent
                        radius: 14
                        color: Qt.alpha(root.backend ? root.backend.accentColor : "#9AA0A6", 0.18)
                        visible: artImage.status !== Image.Ready
                    }

                    Image {
                        id: artImage
                        anchors.fill: parent
                        source: root.hasMedia ? root.backend.art : ""
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        visible: status === Image.Ready
                    }

                    Icon {
                        anchors.centerIn: parent
                        name: "ic_fluent_music_note_2_20_regular"
                        size: 26
                        color: Colors.proxy.textSecondaryColor
                        visible: artImage.status !== Image.Ready
                    }

                    // 播放源图标角标预览：与桌面媒体组件同款样式
                    Rectangle {
                        width: 18
                        height: 18
                        radius: 9
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.rightMargin: -3
                        anchors.bottomMargin: -3
                        visible: root.config("show_source_badge", false)
                                 && root.hasMedia && root.backend.sourceIcon !== ""
                        color: Theme.isDark() ? "#2B2B2B" : "#FFFFFF"
                        border.width: 1
                        border.color: Theme.isDark() ? Qt.alpha("#FFFFFF", 0.18) : Qt.alpha("#000000", 0.12)

                        Image {
                            anchors.fill: parent
                            anchors.margins: 4
                            source: root.hasMedia ? root.backend.sourceIcon : ""
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            smooth: true
                            mipmap: true
                        }
                    }
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Text {
                        Layout.fillWidth: true
                        text: root.hasMedia ? root.backend.title : qsTr("未在播放")
                        typography: Typography.Subtitle
                        elide: Text.ElideRight
                        wrapMode: Text.NoWrap
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.hasMedia && root.backend.artist
                              ? root.backend.artist : qsTr("当前没有正在播放的媒体")
                        typography: Typography.Body
                        color: Colors.proxy.textSecondaryColor
                        elide: Text.ElideRight
                        wrapMode: Text.NoWrap
                    }
                }

                Icon {
                    name: root.backend && root.backend.isPlaying
                          ? "ic_fluent_pause_20_regular" : "ic_fluent_play_20_regular"
                    size: 20
                    color: Colors.proxy.textSecondaryColor
                    visible: root.hasMedia
                }
            }

            // 进度条：专辑主色填充，平滑动画
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 4
                radius: 2
                color: Colors.proxy.controlAltSecondaryColor

                Rectangle {
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: parent.width * (root.backend ? root.backend.progress : 0)
                    radius: 2
                    color: root.backend ? root.backend.accentColor : "#9AA0A6"
                    Behavior on width {
                        NumberAnimation { duration: 250; easing.type: Easing.OutQuad }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true

                Text {
                    text: root.backend ? root.backend.positionText : ""
                    typography: Typography.Caption
                    color: Colors.proxy.textSecondaryColor
                    visible: root.hasMedia
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: root.backend ? root.backend.durationText : ""
                    typography: Typography.Caption
                    color: Colors.proxy.textSecondaryColor
                    visible: root.hasMedia
                }
            }
        }

        // 播放源：应用名 + 图标（卡片右上角，与播放/暂停图标错开高度）
        RowLayout {
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.topMargin: 2
            anchors.rightMargin: 2
            spacing: 6
            visible: root.hasMedia && root.backend
                     && (root.backend.sourceName !== "" || root.backend.sourceIcon !== "")

            Text {
                Layout.maximumWidth: 168
                text: root.hasMedia && root.backend ? root.backend.sourceName : ""
                typography: Typography.Caption
                color: Colors.proxy.textSecondaryColor
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
                visible: text !== ""
            }

            Item {
                Layout.preferredWidth: 16
                Layout.preferredHeight: 16
                visible: root.hasMedia && root.backend && root.backend.sourceIcon !== ""

                Image {
                    anchors.fill: parent
                    anchors.margins: 1
                    source: root.hasMedia ? root.backend.sourceIcon : ""
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    smooth: true
                    mipmap: true
                }
            }
        }
    }

    // ---------- 媒体 ----------

    Text {
        Layout.fillWidth: true
        Layout.topMargin: 20
        typography: Typography.BodyStrong
        text: qsTr("媒体")
    }

    // 媒体组件封面右下角的播放源应用图标角标
    SettingCard {
        Layout.fillWidth: true
        Layout.topMargin: 4
        icon.name: "ic_fluent_apps_20_regular"
        title: qsTr("显示播放源图标")
        description: qsTr("在媒体组件的专辑封面右下角叠加显示正在播放的应用图标")

        Switch {
            checked: root.config("show_source_badge", false)
            onToggled: Configs.setPlugin(root.pluginId, "show_source_badge", checked)
        }
    }

    // ---------- 歌词 ----------

    Text {
        Layout.fillWidth: true
        Layout.topMargin: 20
        typography: Typography.BodyStrong
        text: qsTr("歌词")
    }

    // 歌词源选择：改动立即生效（对当前歌曲重新抓取）
    SettingCard {
        Layout.fillWidth: true
        Layout.topMargin: 4
        icon.name: "ic_fluent_music_note_2_20_regular"
        title: qsTr("歌词源")
        description: qsTr("「自动」按 QQ → 酷狗 → 网易云顺序取第一个匹配，优先逐字歌词")

        ComboBox {
            id: sourceCombo
            textRole: "label"
            model: ListModel {
                ListElement { label: qsTr("自动"); value: "auto" }
                ListElement { label: qsTr("QQ音乐"); value: "qqmusic" }
                ListElement { label: qsTr("酷狗音乐"); value: "kugou" }
                ListElement { label: qsTr("网易云音乐"); value: "netease" }
            }

            property string currentSource: root.config("lyric_source", "auto")
            currentIndex: {
                var idx = 0
                for (var i = 0; i < sourceCombo.count; i++)
                    if (sourceCombo.model.get(i).value === currentSource)
                        idx = i
                return idx
            }
            onActivated: (index) => {
                Configs.setPlugin(root.pluginId, "lyric_source",
                                  sourceCombo.model.get(index).value)
            }
        }
    }

    // 歌词翻译（如有）显示开关
    SettingCard {
        Layout.fillWidth: true
        icon.name: "ic_fluent_translate_20_regular"
        title: qsTr("显示歌词翻译")
        description: qsTr("有翻译时在歌词组件的原文下方显示译文；无翻译或关闭时显示下一行预览")

        Switch {
            checked: root.config("show_translation", true)
            onToggled: Configs.setPlugin(root.pluginId, "show_translation", checked)
        }
    }
}
