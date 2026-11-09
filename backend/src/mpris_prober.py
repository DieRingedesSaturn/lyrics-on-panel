import dbus
import re

from mpris_player import PlaybackStatus, get_session_bus

def find_players():
    """
    Finds all running media players that implement the MPRIS2 interface.
    """
    try:
        bus = get_session_bus()
        playernames = []
        for s in bus.list_names():
            if re.match('org.mpris.MediaPlayer2.', s):
                playernames.append(s)
        return playernames
    except dbus.exceptions.DBusException:
        return []

def find_playing_players(playernames=None):
    '''
    Finds all running media players that implement the MPRIS2 interface, and current playing something.

    Notice:
    From our implementation, the ordering of players is determined solely by their first appearance in
    the MPRIS DBus via `find_players()` and remains stable across playback state changes. Therefore,
    earlier-registered players always take priority whenever they enter the Playing state.
    '''
    bus = get_session_bus()
    if playernames is None:
        playernames = find_players()
    playing_playernames = []
    for playername in playernames:
        try:
            dbusobj = bus.get_object(playername, '/org/mpris/MediaPlayer2')
            props_iface = dbus.Interface(dbusobj, 'org.freedesktop.DBus.Properties')
            playback_status = props_iface.Get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus')
            if playback_status.lower() == PlaybackStatus.PLAYING.value.lower():
                playing_playernames.append(playername)
        except dbus.exceptions.DBusException:
            continue
    return playing_playernames

def probe_players(playernames=None):
    """
    Snapshot every MPRIS player in a single GetAll call per player.

    Returns a list of dicts with the fields the multi-player selection needs:
    name, playing, volume, muted, url and title.
    """
    bus = get_session_bus()
    if playernames is None:
        playernames = find_players()
    players = []
    for playername in playernames:
        try:
            dbusobj = bus.get_object(playername, '/org/mpris/MediaPlayer2')
            props_iface = dbus.Interface(dbusobj, 'org.freedesktop.DBus.Properties')
            data = props_iface.GetAll('org.mpris.MediaPlayer2.Player')
        except dbus.exceptions.DBusException:
            continue
        metadata = data.get('Metadata') or {}
        try:
            volume = float(data.get('Volume', 1.0))
        except (TypeError, ValueError):
            volume = 1.0
        players.append({
            'name': playername,
            'playing': str(data.get('PlaybackStatus', '')).lower() == PlaybackStatus.PLAYING.value.lower(),
            'volume': volume,
            'muted': volume <= 0.0,
            'url': str(metadata.get('xesam:url', '')),
            'title': str(metadata.get('xesam:title', '')),
        })
    return players


