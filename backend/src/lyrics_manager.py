import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from mpris_prober import find_players, probe_players
from mpris_player import MprisPlayer, PlaybackStatus


# Players driven by a browser expose their page URL through MPRIS, while native
# players do not. Muted browser sessions are usually autoplaying previews.
BROWSER_MARKERS = ('firefox', 'chrome', 'chromium', 'brave', 'vivaldi', 'edge', 'opera', 'plasma-browser-integration')
YOUTUBE_CANDIDATE_LANGS = ('en', 'zh-Hans', 'zh-Hant', 'zh')
SUBTITLE_LANG_MODES = ('auto', 'zh', 'en')
BILIBILI_SUB_LANGS = ('zh-CN', 'zh-Hans', 'ai-zh', 'zh')
YOUTUBE_ID = re.compile(r'^[0-9A-Za-z_-]{6,20}$')
SPLAYER_NEXT_PORT = int(os.environ.get('SPLAYER_NEXT_PORT', '14558'))
LIKED_REFRESH_SECONDS = 2.0
SENTENCE_END = re.compile(r'[.!?。！？；;]$')
CAPTION_MAX_LEN = 80
CAPTION_MAX_DURATION_US = 8 * 1000000
# Browsers only push MPRIS Position on events, so a value that stays put is
# trusted as an anchor and the rest is extrapolated locally.
POSITION_RESYNC_US = 1000000

class LyricsManager:
    """
    Core Controller: Manages player selection, tracking, and lyrics fetching.

    Note: ms here == microseconds, not miliseconds.
    """
    def __init__(self):
        self.lyrics_cache = {}
        self._fetch_id = 0
        self._playing_since = {}
        self._playing_prev = set()
        self._pos_anchor = None
        self.liked = False
        self._liked_ts = 0.0
        self._liked_inflight = False
        self._splayer_port = SPLAYER_NEXT_PORT
        self._subtitle_lang = 'auto'
        self._force_refetch = False
        self._fetch_attempts = 0
        self._last_fetch_attempt = 0.0
        self.setup()
    
    
    def setup(self, playername=None, playerobj=None, title=None, artist=None, album=None,
              duration=0, identity=None, lyrics=None, current_lyric=None,
              playback_status=PlaybackStatus.STOPPED, position_ms=0, available_players=None,
              url=None):
        self.playername = playername
        self.playerobj = playerobj
        self.title = title
        self.artist = artist
        self.album = album
        self.duration = duration
        self.identity = identity
        self.lyrics = lyrics
        self.current_lyric = current_lyric
        self.playback_status = playback_status
        self.position_ms = position_ms
        self.available_players = available_players or []
        self.url = url
    
    
    def poll_status(self, requested_playername=None, splayer_next_port=None, subtitle_language=None):
        """
        Polls for player changes and state updates.
        
        requested_playername == None => Global Mode
                        == 'org.mpris.MediaPlayer2.spotify' => Spotify Mode
                        == 'org.mpris.MediaPlayer2.yesplaymusic' => YesPlayMusic Mode
        
        Args:
            requested_playername (str, optional): The specific DBus name to track (e.g. 'org.mpris.MediaPlayer2.spotify').
                                             If None, defaults to the first available player.
        """
        if splayer_next_port:
            try:
                port = int(splayer_next_port)
            except (TypeError, ValueError):
                port = 0
            if 0 < port < 65536:
                self._splayer_port = port
        if subtitle_language in SUBTITLE_LANG_MODES and subtitle_language != self._subtitle_lang:
            self._subtitle_lang = subtitle_language
            # Cached subtitles were picked with the previous preference.
            self.lyrics_cache = {key: value for key, value in self.lyrics_cache.items()
                                 if not key.startswith('video:')}
            self._force_refetch = True
        playernames = find_players()
        # Selection Logic:
            # 1. If there is no avaiable mpris complaint player, return empty State.
            # 2. If a specific target is requested:
                # 2.1 If the requested target exists in the mpris2 dbus interface: pick it.
                # 2.2 Otherwise, return empty state as the requested player is not playing.
            # 3. If no target requested, and then we enter the multiplexing mode. 
                # 3.1 If there is a player with playing status, pick it.
                # 3.2 If there is no player with playing status, fallback to the first player(paused/stopped) exists in mpris dbus.
                    # 3.2.1 If there is no player exists in the mpris dbus, return empty state.
        def set_free():
            self.setup()
            return self._get_empty_state()
        
        if not playernames:
            return set_free()
        current_playername = self._select_player(playernames, requested_playername)
        if not current_playername:
            if requested_playername:
                return self._get_empty_state()
            return set_free()
        current_playerobj = MprisPlayer(current_playername)
        if not current_playerobj.obj:
            return set_free()
        # Cache DBus properties to avoid repeated calls
        try:
            track_info = current_playerobj.track_info
            playback_status = current_playerobj.playback_status
            position = current_playerobj.position
            identity = current_playerobj.identity
            rate = current_playerobj.rate
        except Exception:
            return set_free()
        player_changed = current_playername != self.playername
        track_changed = (self.title != track_info['title']
                or self.artist != track_info['artist']
                or self.album != track_info['album']
                or self._video_info(self.url) != self._video_info(track_info['url']))
        cache_key = self._cache_key(current_playername, track_info['url'])
        if track_changed or player_changed or self._force_refetch:
            self._force_refetch = False
            self._fetch_attempts = 0
            self.title = track_info['title']
            self.artist = track_info['artist']
            self.album = track_info['album']
            # Check if this player/video already has cached lines
            cached = self.lyrics_cache.get(cache_key)
            if (cached and cached['title'] == track_info['title']
                    and cached['artist'] == track_info['artist']
                    and cached['album'] == track_info['album']):
                self.lyrics = cached['lyrics']
            else:
                self.lyrics = None
                self._fetch_id += 1
                self._fetch_attempts += 1
                self._last_fetch_attempt = time.monotonic()
                threading.Thread(target=self._fetch_lyrics, args=(current_playername, track_info, self._fetch_id, cache_key), daemon=True).start()
        elif (self.lyrics is None and self._video_info(track_info['url'])
                and self._fetch_attempts < 3
                and time.monotonic() - self._last_fetch_attempt > 15):
            # YouTube throttles subtitle downloads; give a failed video another try.
            self._fetch_id += 1
            self._fetch_attempts += 1
            self._last_fetch_attempt = time.monotonic()
            threading.Thread(target=self._fetch_lyrics, args=(current_playername, track_info, self._fetch_id, cache_key), daemon=True).start()
        if self._is_splayer_next(current_playername):
            if track_changed or player_changed:
                self.liked = False
                self._liked_ts = 0.0
            if not self._liked_inflight and time.monotonic() - self._liked_ts > LIKED_REFRESH_SECONDS:
                threading.Thread(target=self._refresh_liked, daemon=True).start()
        else:
            self.liked = False
        self.position_ms = self._sync_position(position, playback_status, rate, track_changed or player_changed)
        current_lyric = self._get_current_lyric()
        self.setup(
            playername=current_playername,
            playerobj=current_playerobj,
            title=track_info['title'],
            artist=track_info['artist'],
            album=track_info['album'],
            duration=track_info['length'],
            identity=identity,
            lyrics=self.lyrics,
            current_lyric=current_lyric,
            playback_status=playback_status,
            position_ms=self.position_ms,
            available_players=playernames,
            url=track_info['url']
        )
        return self.get_state()


    def _resolve_player_name(self, name, playernames):
        """Accept a bare bus name such as 'org.mpris.MediaPlayer2.splayer_next'."""
        if not name:
            return None
        if name in playernames:
            return name
        for playername in playernames:
            if playername.startswith(name + '.'):
                return playername
        return None


    def resolve_player_name(self, name):
        return self._resolve_player_name(name, find_players())


    def _is_splayer_next(self, playername):
        return 'splayer_next' in (playername or '').lower()


    def _splayer_url(self, path):
        return f'http://127.0.0.1:{self._splayer_port}{path}'


    def _refresh_liked(self):
        self._liked_inflight = True
        try:
            data = self._api_get_json(self._splayer_url('/api/favorite'))
            if data and isinstance(data.get('liked'), bool):
                self.liked = data['liked']
            self._liked_ts = time.monotonic()
        finally:
            self._liked_inflight = False


    def toggle_favorite(self, playername=None):
        """Toggle the favourite state through SPlayer-Next's local API."""
        name = self.resolve_player_name(playername or self.playername or '') or ''
        if not self._is_splayer_next(name):
            return False
        liked = not self.liked
        body = json.dumps({'liked': liked}).encode('utf-8')
        request = urllib.request.Request(
            self._splayer_url('/api/favorite'), data=body,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                if resp.status != 200:
                    return False
        except Exception:
            return False
        self.liked = liked
        # The player may need to reach a remote service; let the poll loop confirm.
        self._liked_ts = 0.0
        return True


    def _is_browser(self, playername):
        name = playername.lower()
        return any(marker in name for marker in BROWSER_MARKERS)


    def _select_player(self, playernames, requested_playername=None):
        """
        Pick the player that is actually playing.

        Explicit request wins. Otherwise a playing player beats a paused one,
        and the most recently started playback wins a tie. Browser players that
        are muted are skipped: autoplaying previews keep a silent session open.
        """
        if requested_playername:
            return self._resolve_player_name(requested_playername, playernames)
        try:
            infos = probe_players(playernames)
        except Exception:
            return self.playername if self.playername in playernames else playernames[0]
        if not infos:
            return None
        now = time.monotonic()
        playing_now = {info['name'] for info in infos if info['playing']}
        for name in playing_now - self._playing_prev:
            self._playing_since[name] = now
        self._playing_prev = playing_now
        audible = [info for info in infos
                   if not (self._is_browser(info['name']) and info['muted'])]
        if not audible:
            audible = infos
        by_name = {info['name']: info for info in audible}
        playing = [info for info in audible if info['playing']]
        if playing:
            if self.playername in by_name and by_name[self.playername]['playing']:
                return self.playername
            playing.sort(key=lambda info: self._playing_since.get(info['name'], 0), reverse=True)
            return playing[0]['name']
        if self.playername in by_name:
            return self.playername
        return audible[0]['name']


    def _sync_position(self, raw_position, playback_status, rate, reset=False):
        """
        Resynchronise the playback position.

        Browser MPRIS implementations only refresh Position on events, so while
        nothing new arrives the position is extrapolated from the last anchor.
        Any movement in the raw value (seek or a fresh push) re-anchors it.
        """
        now = time.monotonic()
        playing = playback_status == PlaybackStatus.PLAYING
        if reset or self._pos_anchor is None:
            self._pos_anchor = (raw_position, now, rate, playing)
            return raw_position
        anchor_position, anchor_time, anchor_rate, anchor_playing = self._pos_anchor
        if playing != anchor_playing:
            self._pos_anchor = (raw_position, now, rate, playing)
            return raw_position
        if abs(raw_position - anchor_position) > POSITION_RESYNC_US:
            self._pos_anchor = (raw_position, now, rate, playing)
            return raw_position
        if not playing:
            return anchor_position
        estimated = anchor_position + int((now - anchor_time) * 1000000 * max(anchor_rate, 0))
        if self.duration and estimated > self.duration:
            estimated = self.duration
        return estimated


    def _video_info(self, url):
        """Return {'site', 'id', 'page'} when the URL points at a video page."""
        if not url:
            return None
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = (parsed.netloc or '').lower()
        path = parsed.path or ''
        on_youtube = host == 'youtube.com' or host.endswith('.youtube.com')
        if host == 'youtu.be' or (on_youtube and not host.startswith('music.')):
            if path.startswith('/watch'):
                video_id = (parse_qs(parsed.query).get('v') or [''])[0]
            else:
                video_id = path.strip('/').split('/')[-1] if path.strip('/') else ''
            if not YOUTUBE_ID.match(video_id):
                return None
            return {'site': 'youtube', 'id': video_id}
        if host == 'bilibili.com' or host.endswith('.bilibili.com'):
            match = re.search(r'/video/(BV[0-9A-Za-z]+|av\d+)', path)
            if not match:
                return None
            page = (parse_qs(parsed.query).get('p') or ['1'])[0]
            return {'site': 'bilibili', 'id': match.group(1),
                    'page': int(page) if page.isdigit() else 1}
        return None


    def _cache_key(self, playername, url):
        video = self._video_info(url)
        if not video:
            return playername
        key = f"video:{video['site']}:{video['id']}"
        if video.get('page'):
            key = f"{key}:{video['page']}"
        return key


    def _fetch_lyrics(self, playername, track_info, fetch_id, cache_key):
        # Check if this fetch is still current
        if self._fetch_id != fetch_id:
            return
        title = track_info['title']
        artists = track_info['artist']
        artist = artists[0] if artists else ''
        album = track_info['album']
        length = track_info['length']
        url = track_info['url']
        video = self._video_info(url)
        if video is None and (not title or not artists):
            return
        try:
            lyrics = None
            if video:
                lyrics = self._fetch_video_lyrics(video)
            elif self._is_splayer_next(playername):
                lyrics = self._fetch_lyrics_splayer_next(title)
            elif playername == 'org.mpris.MediaPlayer2.yesplaymusic':
                lyrics = self._fetch_lyrics_ypm(title)
            elif playername == 'org.mpris.MediaPlayer2.lx-music-desktop':
                lyrics = self._fetch_lyrics_lxmusic()
            else:
                lyrics = self._fetch_lyrics_local(url)
                if lyrics is None:
                    # Check again before expensive HTTP calls
                    if self._fetch_id != fetch_id:
                        return
                    lyrics = self._fetch_lyrics_lrclib(title, artist, album, length)
            # Only write if this fetch is still current
            if self._fetch_id == fetch_id:
                self.lyrics = lyrics
                if lyrics:
                    self.lyrics_cache[cache_key] = {
                        'title': title,
                        'artist': artists,
                        'album': album,
                        'lyrics': lyrics
                    }
        except Exception as e:
            if self._fetch_id == fetch_id:
                self.lyrics = None


    def _fetch_video_lyrics(self, video):
        if video['site'] == 'youtube':
            return self._fetch_youtube_subtitles(video['id'])
        if video['site'] == 'bilibili':
            return self._fetch_bilibili_subtitles(video['id'], video.get('page', 1))
        return None


    def _api_get_json(self, url, referer=None):
        headers = {'User-Agent': 'Mozilla/5.0'}
        if referer:
            headers['Referer'] = referer
        sessdata = os.environ.get('BILI_SESSDATA')
        if sessdata and 'bilibili.com' in url:
            headers['Cookie'] = f'SESSDATA={sessdata}'
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=8) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception:
            return None


    def _fetch_youtube_subtitles(self, video_id):
        """YouTube's timedtext endpoint needs a signature, so let yt-dlp do the work."""
        with tempfile.TemporaryDirectory() as tmp:
            command = [
                'yt-dlp',
                '--skip-download', '--write-info-json', '--write-subs', '--write-auto-subs',
                '--no-playlist',
                '--sub-langs', ','.join(YOUTUBE_CANDIDATE_LANGS),
                '--sub-format', 'json3', '--no-warnings', '--ignore-errors', '--retries', '2',
                '-o', str(Path(tmp) / '%(id)s.%(ext)s'),
                f'https://www.youtube.com/watch?v={video_id}',
            ]
            try:
                subprocess.run(command, capture_output=True, timeout=30)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                return None
            files = {}
            for path in Path(tmp).glob('*.json3'):
                # <video id>.<language>.json3
                language = path.name[len(video_id) + 1:-len('.json3')]
                files[language] = path
            if not files:
                return None
            info = {}
            info_path = Path(tmp) / f'{video_id}.info.json'
            try:
                info = json.loads(info_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                info = {}
            chosen = self._pick_youtube_subtitle(files, info)
            if not chosen:
                return None
            try:
                data = json.loads(chosen.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None
            return self._merge_captions(self._parse_json3(data))


    def _pick_youtube_subtitle(self, files, info):
        """
        Choose a downloaded subtitle track.

        'auto' follows the spoken language when it is Chinese or English and
        falls back to Chinese otherwise; the explicit modes override that.
        """
        if not files:
            return None
        source = str(info.get('language') or '').lower()
        primary = source.split('-')[0]
        if self._subtitle_lang == 'zh':
            order = ['zh-Hans', 'zh-Hant', 'zh', 'en']
        elif self._subtitle_lang == 'en':
            order = ['en', 'zh-Hans', 'zh-Hant', 'zh']
        elif primary == 'zh':
            if 'hant' in source or source.endswith('-tw') or source.endswith('-hk'):
                order = ['zh-Hant', 'zh-Hans', 'zh', 'en']
            else:
                order = ['zh-Hans', 'zh-Hant', 'zh', 'en']
        elif primary == 'en':
            order = ['en', 'zh-Hans', 'zh-Hant', 'zh']
        else:
            order = ['zh-Hans', 'zh-Hant', 'zh', 'en']
        for language in order:
            if language in files:
                return files[language]
        return next(iter(files.values()))


    def _parse_json3(self, data):
        lines = []
        for event in data.get('events') or []:
            # Events flagged aAppend only carry a line break from the previous line.
            if event.get('aAppend') or not event.get('segs'):
                continue
            text = ''.join(seg.get('utf8', '') for seg in event['segs']).strip()
            if not text:
                continue
            lines.append({'time_ms': int(event.get('tStartMs', 0)) * 1000, 'lyric': text})
        return lines


    def _join_caption(self, left, right):
        if left and right and '\u4e00' <= left[-1] <= '\u9fff':
            return left + right
        return f'{left} {right}'


    def _merge_captions(self, lines):
        """Auto captions arrive as 2-4 word fragments; glue them into readable lines."""
        merged = []
        buffer = None
        for line in lines:
            if buffer is None:
                buffer = dict(line)
                continue
            too_long = len(buffer['lyric']) + len(line['lyric']) > CAPTION_MAX_LEN
            too_slow = line['time_ms'] - buffer['time_ms'] > CAPTION_MAX_DURATION_US
            if too_long or too_slow:
                merged.append(buffer)
                buffer = dict(line)
                continue
            buffer['lyric'] = self._join_caption(buffer['lyric'], line['lyric'])
            if SENTENCE_END.search(line['lyric']):
                merged.append(buffer)
                buffer = None
        if buffer:
            merged.append(buffer)
        return merged


    def _fetch_bilibili_subtitles(self, video_id, page=1):
        """
        Bilibili keeps subtitles behind its player API. AI subtitles need a
        logged-in SESSDATA cookie, uploader-provided CC works anonymously.
        """
        query = f'aid={video_id[2:]}' if video_id.lower().startswith('av') else f'bvid={video_id}'
        referer = f'https://www.bilibili.com/video/{video_id}'
        view = self._api_get_json(f'https://api.bilibili.com/x/web-interface/view?{query}', referer)
        if not view or view.get('code') != 0:
            return None
        data = view.get('data') or {}
        cid = 0
        pages = data.get('pages') or []
        if 0 < page <= len(pages):
            cid = pages[page - 1].get('cid') or 0
        cid = cid or data.get('cid')
        if not cid:
            return None
        player = self._api_get_json(
            f'https://api.bilibili.com/x/player/v2?{query}&cid={cid}', referer)
        if not player or player.get('code') != 0:
            return None
        subtitles = ((player.get('data') or {}).get('subtitle') or {}).get('subtitles') or []
        if not subtitles:
            return None
        chosen = None
        for lang in BILIBILI_SUB_LANGS:
            for subtitle in subtitles:
                if subtitle.get('lan') == lang:
                    chosen = subtitle
                    break
            if chosen:
                break
        chosen = chosen or subtitles[0]
        subtitle_url = chosen.get('subtitle_url') or ''
        if subtitle_url.startswith('//'):
            subtitle_url = 'https:' + subtitle_url
        payload = self._api_get_json(subtitle_url, referer)
        if not payload:
            return None
        lines = []
        for item in payload.get('body') or []:
            text = (item.get('content') or '').strip()
            if not text:
                continue
            lines.append({'time_ms': int(float(item.get('from', 0)) * 1000000), 'lyric': text})
        return lines or None


    def _http_get(self, url, timeout=5):
        """Simple HTTP GET using urllib. Returns (status_code, data) tuple."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status, resp.read().decode('utf-8')
        except Exception:
            return None, None

    def _fetch_lyrics_splayer_next(self, title):
        """
        SPlayer-Next exposes the current track and its lyrics over a local API.

        Newer builds answer with a full lyric list inside the now-playing
        snapshot; older ones only advertise lyricAvailable and expect a second
        request to /api/lyrics.
        """
        snapshot = self._api_get_json(self._splayer_url('/api/now-playing'))
        if not snapshot:
            return None
        track = snapshot.get('track') or {}
        if title and track.get('title') and track['title'] != title:
            return None
        if snapshot.get('lyric'):
            return self._parse_splayer_lines(snapshot['lyric'])
        if snapshot.get('lyricAvailable') is False:
            return None
        payload = self._api_get_json(self._splayer_url('/api/lyrics'))
        if not payload:
            return None
        track_id = payload.get('trackId')
        if track_id and track.get('id') and str(track_id) != str(track['id']):
            return None
        return self._parse_splayer_lines(payload.get('lyric') or [])


    def _parse_splayer_lines(self, lines):
        parsed = []
        for line in lines or []:
            if not line or line.get('isBG'):
                continue
            text = ''.join((word or {}).get('word', '') for word in line.get('words') or []).strip()
            if not text:
                continue
            # SPlayer-Next reports milliseconds, this project works in microseconds.
            parsed.append({'time_ms': int(float(line.get('startTime', 0)) * 1000), 'lyric': text})
        return parsed or None


    def _fetch_lyrics_ypm(self, title):
        """Fetch lyrics from YesPlayMusic localhost API. Returns parsed lyrics or None."""
        ypm_base_url = "http://localhost:27232"
        status, text = self._http_get(f"{ypm_base_url}/player")
        if status != 200:
            return None
        data = json.loads(text)
        if not data or not data.get('currentTrack') or data['currentTrack'].get('name') != title:
            return None
        track_id = data['currentTrack']['id']
        status, text = self._http_get(f"{ypm_base_url}/api/lyric?id={track_id}")
        if status != 200:
            return None
        data = json.loads(text)
        if data and data.get('lrc') and data['lrc'].get('lyric'):
            return self._parse_lrc(data['lrc']['lyric'])
        return None

    def _fetch_lyrics_lxmusic(self, port=23330):
        """Fetch lyrics from LX Music localhost API. Returns parsed lyrics or None."""
        lxmusic_base_url = f"http://localhost:{port}"
        status, text = self._http_get(f"{lxmusic_base_url}/lyric")
        if status == 200 and text:
            return self._parse_lrc(text)
        return None

    def _fetch_lyrics_local(self, song_path):
        """Fetch lyrics from disk."""
        if not song_path.startswith('file://'):
            return None
        lrc_path = Path(unquote(urlparse(song_path).path)).with_suffix('.lrc')
        try:
            content = lrc_path.read_text(encoding='utf-8')
        except (FileNotFoundError, PermissionError, OSError):
            return None
        return self._parse_lrc(content)

    def _fetch_lyrics_lrclib(self, title, artist, album, length):
        """Fetch lyrics from lrclib.net (compatible mode). Returns parsed lyrics or None."""
        duration_sec = length // 1000000 if length else None
        params = urlencode({
            'track_name': title,
            'artist_name': artist,
            'album_name': album
        })

        def fetch_exact():
            if not duration_sec:
                return None
            url = f"https://lrclib.net/api/get?{params}&duration={duration_sec}"
            status, text = self._http_get(url)
            if status == 200 and text:
                data = json.loads(text)
                return data.get('syncedLyrics')
            return None

        def fetch_search():
            url = f"https://lrclib.net/api/search?{params}"
            status, text = self._http_get(url)
            if status == 200 and text:
                data = json.loads(text)
                for result in data:
                    if result.get('syncedLyrics'):
                        return result['syncedLyrics']
            return None

        def fetch_fuzzy():
            url = f"https://lrclib.net/api/search?q={quote(title)}"
            status, text = self._http_get(url)
            if status == 200 and text:
                data = json.loads(text)
                for result in data:
                    if result.get('syncedLyrics'):
                        return result['syncedLyrics']
            return None

        # Run all fetches in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(fetch_exact): 0,
                executor.submit(fetch_search): 1,
                executor.submit(fetch_fuzzy): 2,
            }
            results = [None, None, None]
            for future in as_completed(futures):
                priority = futures[future]
                try:
                    results[priority] = future.result()
                except Exception:
                    pass
        # Pick best result by priority
        for result in results:
            if result:
                return self._parse_lrc(result)
        return None


    def _parse_lrc(self, lrc_text):
        lines = []
        for line in lrc_text.splitlines():
            parts = line.split(']')
            if len(parts) > 1:
                time_str = parts[0].replace('[', '').strip()
                lyric = parts[1].strip()
                try:
                    m, s = time_str.split(':')
                    time_ms = int((float(m) * 60 + float(s)) * 1000000)
                    lines.append({"time_ms": time_ms, "lyric": lyric})
                except:
                    continue
        return lines


    def _get_current_lyric(self):
        if not self.lyrics:
            return None
        lyrics_line_num = len(self.lyrics)
        start = 0
        end = lyrics_line_num - 1
        while start <= end:
            mid = (start + end) >> 1
            if self.position_ms == self.lyrics[mid]['time_ms']:
                end = mid
                break
            if self.position_ms > self.lyrics[mid]['time_ms']:
                start = mid + 1
            else:
                end = mid - 1
        if end < 0:
            return None
        # If current lyric is empty, find the previous non-empty one
        while end >= 0 and not self.lyrics[end]['lyric']:
            end -= 1
        if end < 0:
            return None
        return self.lyrics[end]['lyric']       
        
        
    def get_state(self):
        if not self.playerobj:
            return self._get_empty_state()
        return {
            "playback_status": self.playback_status.value.lower(),
            "player": {
                "identity": self.identity,
                "bus_name": self.playername
            },
            "track": {
                "title": self.title,
                "artist": ", ".join(self.artist) if self.artist else "",
                "album": self.album,
                "duration": self.duration
            },
            "position_ms": self.position_ms,
            "liked": self.liked,
            "lyrics": {
                'current_lyric': self.current_lyric,
            },
            "available_players": self.available_players
        }


    def _get_empty_state(self):
        return {
            "playback_status": PlaybackStatus.STOPPED.value.lower(),
            "player": None,
            "track": None,
            "position_ms": 0,
            "liked": False,
            "lyrics": None,
            "avaiable_players": None
        }