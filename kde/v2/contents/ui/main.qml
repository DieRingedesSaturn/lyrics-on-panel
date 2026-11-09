import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.1
import QtWebSockets

import org.kde.plasma.core 2.0 as PlasmaCore
import org.kde.plasma.plasmoid 2.0
import org.kde.plasma.components 3.0 as PlasmaComponents
import org.kde.kirigami 2.20 as Kirigami

/**
 * Lyrics on Panel for KDE Plasma 6 - v2.0.0
 *
 * This version uses a WebSocket connection to communicate with a Python backend
 * server that handles MPRIS2 interactions and lyrics fetching.
 *
 * Backend endpoints:
 *   ws://127.0.0.1:23560/poll    - Poll for player state and lyrics
 *   ws://127.0.0.1:23560/control - Send playback control commands
 */

PlasmoidItem {
    id: root

    width: 0
    height: lyricText.contentHeight

    preferredRepresentation: fullRepresentation
    Layout.preferredWidth: config_preferedWidgetWidth
    Layout.preferredHeight: lyricText.contentHeight

    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground | PlasmaCore.Types.ConfigurableBackground

    Plasmoid.status: hasActivePlayer || !config_hideItemWhenNoControlChecked
        ? PlasmaCore.Types.ActiveStatus
        : PlasmaCore.Types.HiddenStatus

    RowLayout {
        anchors.fill: parent
        spacing: config_mediaControllSpacing

        Item {
            id: lyricTextContainer
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            onWidthChanged: {
                if (lyricBounceAnimation.running) {
                    lyricBounceAnimation.stop()
                    restartTimer.start()
                }
            }

            TextMetrics {
                id: lyricTextMetrics
                font: lyricText.font
                text: currentLyric || lrc_not_exists
            }

            SequentialAnimation {
                id: lyricBounceAnimation
                running: lyricTextMetrics.width > lyricTextContainer.width && playbackStatus === "playing"
                loops: Animation.Infinite

                PropertyAnimation {
                    target: lyricText
                    property: "xPosition"
                    from: 0
                    to: lyricTextContainer.width - lyricTextMetrics.width
                    duration: animationDuration
                    easing.type: Easing.Linear
                }

                PauseAnimation { duration: 1000 }

                PropertyAnimation {
                    target: lyricText
                    property: "xPosition"
                    from: lyricTextContainer.width - lyricTextMetrics.width
                    to: 0
                    duration: animationDuration
                    easing.type: Easing.Linear
                }

                PauseAnimation { duration: 1000 }
            }

            Timer {
                id: restartTimer
                interval: 50
                running: false
                repeat: false
                onTriggered: {
                    if (lyricTextMetrics.width > lyricTextContainer.width && playbackStatus === "playing") {
                        lyricText.xPosition = 0
                        lyricBounceAnimation.start()
                    }
                }
            }

            Text {
                id: lyricText
                text: currentLyric || lrc_not_exists
                color: effectiveLyricTextColor
                font.pixelSize: config_lyricTextSize
                font.bold: config_lyricTextBold
                font.italic: config_lyricTextItalic
                anchors.verticalCenter: parent.verticalCenter
                anchors.verticalCenterOffset: config_lyricTextVerticalOffset
                
                property real initialXPosition: {
                    if (config_lyricTextAlignment === 0) {
                        return 0
                    } else if (config_lyricTextAlignment === 1) {
                        return (lyricTextContainer.width - lyricTextMetrics.width) / 2
                    } else {
                        return lyricTextContainer.width - lyricTextMetrics.width
                    }
                }
                
                property real xPosition: initialXPosition
                
                x: lyricTextMetrics.width <= lyricTextContainer.width || !lyricBounceAnimation.running
                    ? initialXPosition
                    : xPosition

                onTextChanged: {
                    lyricBounceAnimation.stop()
                    restartTimer.start()
                }
            }
        }

        RowLayout {
            id: iconsContainer
            Layout.preferredWidth: 5 * config_mediaControllItemSize + 4 * config_mediaControllSpacing
            Layout.preferredHeight: config_mediaControllItemSize
            Layout.alignment: Qt.AlignVCenter
            Layout.rightMargin: 1
            spacing: config_mediaControllSpacing

            Image {
                source: backwardIcon
                sourceSize.width: config_mediaControllItemSize
                sourceSize.height: config_mediaControllItemSize

                MouseArea {
                    anchors.fill: parent
                    onClicked: sendControl("previous")
                }
            }

            Image {
                source: (playbackStatus === "playing") ? pauseIcon : playIcon
                sourceSize.width: config_mediaControllItemSize
                sourceSize.height: config_mediaControllItemSize

                MouseArea {
                    anchors.fill: parent
                    onClicked: sendControl("play_pause")
                }
            }

            Image {
                source: forwardIcon
                sourceSize.width: config_mediaControllItemSize
                sourceSize.height: config_mediaControllItemSize

                MouseArea {
                    anchors.fill: parent
                    onClicked: sendControl("next")
                }
            }

            Image {
                source: liked ? likedIcon : likeIcon
                sourceSize.width: config_mediaControllItemSize
                sourceSize.height: config_mediaControllItemSize

                MouseArea {
                    anchors.fill: parent
                    onClicked: sendControl("favorite")
                }
            }

            Item {
                id: mediaPlayerIcon
                implicitWidth: config_mediaControllItemSize
                implicitHeight: config_mediaControllItemSize

                Image {
                    anchors.fill: parent
                    visible: !isBrowserPlayer
                    source: playerIconSource
                    sourceSize.width: config_mediaControllItemSize
                    sourceSize.height: config_mediaControllItemSize
                }

                Kirigami.Icon {
                    anchors.fill: parent
                    visible: isBrowserPlayer
                    source: "internet-web-browser"
                    color: useWhiteMediaControlIcons ? "white" : Kirigami.Theme.textColor
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        if (availablePlayers.length > 0) {
                            var pos = mediaPlayerIcon.mapToGlobal(0, mediaPlayerIcon.height)
                            playerPopup.x = pos.x - playerPopup.width + mediaPlayerIcon.width
                            playerPopup.y = pos.y + 5
                            playerPopup.visible = !playerPopup.visible
                        }
                    }
                }
            }
        }
    }

    PlasmaCore.Dialog {
        id: playerPopup
        location: PlasmaCore.Types.Floating
        type: PlasmaCore.Dialog.PopupMenu
        flags: Qt.WindowStaysOnTopHint
        hideOnWindowDeactivate: true

        mainItem: ListView {
            id: playerList
            width: 250
            height: Math.min(contentHeight, 300)
            model: availablePlayers
            spacing: 2
            clip: true

            delegate: Rectangle {
                width: playerList.width
                height: 36
                color: delegateMouseArea.containsMouse ? Kirigami.Theme.hoverColor : "transparent"
                radius: 4

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12

                    Text {
                        text: modelData === currentPlayerBusName ? "\u25CF" : "\u25CB"
                        color: modelData === currentPlayerBusName ? "#4CAF50" : "#888"
                        font.pixelSize: 10
                    }

                    Text {
                        text: modelData.replace("org.mpris.MediaPlayer2.", "")
                        color: Kirigami.Theme.textColor
                        font.pixelSize: config_lyricTextSize
                        Layout.fillWidth: true
                    }
                }

                MouseArea {
                    id: delegateMouseArea
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        selectedPlayer = modelData
                        playerPopup.visible = false
                    }
                }
            }
        }
    }

    property string backwardIcon: useWhiteMediaControlIcons ? "../assets/media-backward-white.svg" : "../assets/media-backward.svg"
    property string pauseIcon: useWhiteMediaControlIcons ? "../assets/media-pause-white.svg" : "../assets/media-pause.svg"
    property string forwardIcon: useWhiteMediaControlIcons ? "../assets/media-forward-white.svg" : "../assets/media-forward.svg"
    property string likeIcon: useWhiteMediaControlIcons ? "../assets/media-like-white.svg" : "../assets/media-like.svg"
    property string likedIcon: "../assets/media-liked.svg"
    property string cloudMusicIcon: useWhiteMediaControlIcons ? "../assets/netease-cloud-music-white.svg" : "../assets/netease-cloud-music.svg"
    property string spotifyIcon: useWhiteMediaControlIcons ? "../assets/spotify-white.svg" : "../assets/spotify.svg"
    property string splayerIcon: "../assets/splayer.svg"
    readonly property bool isBrowserPlayer: /firefox|chrome|chromium|brave|vivaldi|edge|opera/.test(currentPlayerBusName.toLowerCase())
    readonly property string playerIconSource: {
        if (config_yesPlayMusicChecked || currentPlayerIdentity.indexOf("YesPlayMusic") === 0) {
            return cloudMusicIcon
        }
        if (config_splayerChecked || config_splayerNextChecked
                || currentPlayerIdentity.indexOf("SPlayer") === 0) {
            return splayerIcon
        }
        return spotifyIcon
    }
    property string playIcon: useWhiteMediaControlIcons ? "../assets/media-play-white.svg" : "../assets/media-play.svg"
    property bool liked: false

    property bool config_yesPlayMusicChecked: Plasmoid.configuration.yesPlayMusicChecked
    property bool config_lxMusicChecked: Plasmoid.configuration.lxMusicChecked
    property bool config_splayerChecked: Plasmoid.configuration.splayerChecked
    property bool config_splayerNextChecked: Plasmoid.configuration.splayerNextChecked
    property bool config_spotifyChecked: Plasmoid.configuration.spotifyChecked
    property bool config_compatibleModeChecked: Plasmoid.configuration.compatibleModeChecked
    property bool config_followSystemColorSchemeChecked: Plasmoid.configuration.followSystemColorSchemeChecked
    property color effectiveLyricTextColor: config_followSystemColorSchemeChecked ? Kirigami.Theme.textColor : config_lyricTextColor
    property bool useWhiteMediaControlIcons: config_followSystemColorSchemeChecked ? isLightColor(Kirigami.Theme.textColor) : config_whiteMediaControlIconsChecked

    property int config_lyricTextSize: Plasmoid.configuration.lyricTextSize
    property string config_lyricTextColor: Plasmoid.configuration.lyricTextColor
    property bool config_lyricTextBold: Plasmoid.configuration.lyricTextBold
    property bool config_lyricTextItalic: Plasmoid.configuration.lyricTextItalic
    property int config_lyricTextVerticalOffset: Plasmoid.configuration.lyricTextVerticalOffset
    property int config_lyricTextAlignment: Plasmoid.configuration.lyricTextAlignment

    property int config_mediaControllSpacing: Plasmoid.configuration.mediaControllSpacing
    property int config_mediaControllItemSize: Plasmoid.configuration.mediaControllItemSize
    property int config_mediaControllItemVerticalOffset: Plasmoid.configuration.mediaControllItemVerticalOffset

    property bool config_whiteMediaControlIconsChecked: Plasmoid.configuration.whiteMediaControlIconsChecked
    property int config_preferedWidgetWidth: Plasmoid.configuration.preferedWidgetWidth
    property bool config_hideItemWhenNoControlChecked: Plasmoid.configuration.hideItemWhenNoControlChecked

    property int config_lxMusicPort: Plasmoid.configuration.lxMusicPort
    property int config_splayerNextPort: Plasmoid.configuration.splayerNextPort
    property string config_subtitleLanguage: {
        var modes = ["auto", "zh", "en"]
        return modes[Plasmoid.configuration.subtitleLanguage] || "auto"
    }

    readonly property string serverHost: "127.0.0.1"
    readonly property int serverPort: 23560

    property string playbackStatus: "stopped"
    property string currentLyric: ""
    property string currentTitle: ""
    property string currentArtist: ""
    property string currentAlbum: ""
    property string currentPlayerIdentity: ""
    property string currentPlayerBusName: ""
    property int positionMs: 0
    property bool hasActivePlayer: false
    property var availablePlayers: []
    property string selectedPlayer: ""
    property int animationDuration: Math.max(2000, Math.abs((lyricTextContainer.width - lyricTextMetrics.width) / 50 * 1000))

    property string requestedPlayer: {
        if (selectedPlayer) {
            return selectedPlayer
        } else if (config_yesPlayMusicChecked) {
            return "org.mpris.MediaPlayer2.yesplaymusic"
        } else if (config_spotifyChecked) {
            return "org.mpris.MediaPlayer2.spotify"
        } else if (config_lxMusicChecked) {
            return "org.mpris.MediaPlayer2.lx-music-desktop"
        } else if (config_splayerNextChecked) {
            return "org.mpris.MediaPlayer2.splayer_next"
        } else if (config_splayerChecked) {
            return "org.mpris.MediaPlayer2.splayer"
        } else {
            return ""
        }
    }

    property string lrc_not_exists: {
        if (currentTitle && currentArtist) {
            return currentTitle + " - " + currentArtist
        } else if (currentTitle) {
            return currentTitle
        } else {
            return " "
        }
    }

    WebSocket {
        id: pollSocket
        url: "ws://" + serverHost + ":" + serverPort + "/poll"
        active: true

        onStatusChanged: {
            if (pollSocket.status === WebSocket.Open) {
                console.log("Poll WebSocket connected")
                sendPollRequest()
            } else if (pollSocket.status === WebSocket.Closed) {
                console.log("Poll WebSocket closed, reconnecting...")
                hasActivePlayer = false
                currentLyric = ""
                reconnectTimer.start()
            } else if (pollSocket.status === WebSocket.Error) {
                console.log("Poll WebSocket error:", pollSocket.errorString)
                hasActivePlayer = false
                reconnectTimer.start()
            }
        }

        onTextMessageReceived: function(message) {
            try {
                var data = JSON.parse(message)
                handlePollResponse(data)
                sendPollRequest()
            } catch (e) {
                console.log("Error parsing poll response:", e)
            }
        }
    }

    WebSocket {
        id: controlSocket
        url: "ws://" + serverHost + ":" + serverPort + "/control"
        active: true

        onStatusChanged: {
            if (controlSocket.status === WebSocket.Open) {
                console.log("Control WebSocket connected")
            } else if (controlSocket.status === WebSocket.Error) {
                console.log("Control WebSocket error:", controlSocket.errorString)
            }
        }

        onTextMessageReceived: function(message) {
            try {
                var data = JSON.parse(message)
                if (!data.success) {
                    console.log("Control command failed")
                }
            } catch (e) {
                console.log("Error parsing control response:", e)
            }
        }
    }

    function sendPollRequest() {
        if (pollSocket.status === WebSocket.Open) {
            var request = {
                "player": requestedPlayer || null,
                "splayer_next_port": config_splayerNextPort,
                "subtitle_language": config_subtitleLanguage
            }
            pollSocket.sendTextMessage(JSON.stringify(request))
        }
    }

    Timer {
        id: reconnectTimer
        interval: 3000
        running: false
        repeat: false
        onTriggered: {
            if (pollSocket.status !== WebSocket.Open) {
                pollSocket.active = false
                pollSocket.active = true
            }
            if (controlSocket.status !== WebSocket.Open) {
                controlSocket.active = false
                controlSocket.active = true
            }
        }
    }

    function isLightColor(colorValue) {
        // Plasma dark themes usually hand out a light text colour, and vice versa.
        return (0.2126 * colorValue.r + 0.7152 * colorValue.g + 0.0722 * colorValue.b) > 0.5
    }

    function handlePollResponse(data) {
        if (!data || !data.player) {
            hasActivePlayer = false
            liked = false
            currentLyric = ""
            currentTitle = ""
            currentArtist = ""
            currentAlbum = ""
            playbackStatus = "stopped"
            return
        }

        hasActivePlayer = true
        playbackStatus = data.playback_status || "stopped"
        positionMs = data.position_ms || 0
        liked = data.liked === true

        if (data.player) {
            currentPlayerIdentity = data.player.identity || ""
            currentPlayerBusName = data.player.bus_name || ""
        }

        if (data.track) {
            currentTitle = data.track.title || ""
            currentArtist = data.track.artist || ""
            currentAlbum = data.track.album || ""
        }

        if (data.lyrics && data.lyrics.current_lyric) {
            currentLyric = data.lyrics.current_lyric
        } else {
            currentLyric = ""
        }

        if (data.available_players) {
            availablePlayers = data.available_players
        }
    }

    function sendControl(action) {
        if (controlSocket.status !== WebSocket.Open) {
            console.log("Control socket not connected")
            return
        }
        var request = {
            "action": action,
            "player": requestedPlayer || currentPlayerBusName || null
        }
        controlSocket.sendTextMessage(JSON.stringify(request))
    }
}
