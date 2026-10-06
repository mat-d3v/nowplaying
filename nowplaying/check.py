"""python3 mpd-bridge.py --check: tests the setup, says what's wrong and how
to fix it. And --lastfm-login, which lets the bridge scrobble to Last.fm.
"""
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION, artwork, bluetooth, config, history, mpd, status, updates
from .audio import describe_badges, display_title, get_alsa_format, get_audio_format, get_codec

RASPOTIFY_CONF = '/etc/raspotify/conf'  # raspotify's settings for librespot


def check():
    # Exit status 1 when something needs fixing
    problems = 0

    def report(state, text, hint=''):
        nonlocal problems
        problems += state == 'FAIL'
        print(f'  {state:<4}  {text}')
        if hint:
            print(f'        {hint}')

    def fetch_json(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if '127.0.0.1' in url \
            else urllib.request.build_opener()
        try:
            with opener.open(url, timeout=8) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b'{}')

    print(f'nowplaying {VERSION} - setup check\n')
    report('ok' if sys.version_info >= (3, 7) else 'FAIL', f'Python {sys.version.split()[0]}',
           '' if sys.version_info >= (3, 7) else 'Python 3.7 or later is needed')
    env_file = os.path.join(config.SCRIPT_DIR, '.env')
    report('ok' if os.path.exists(env_file) else '--',
           f'Settings from {env_file}' if os.path.exists(env_file) else 'No .env file: defaults and environment variables only')

    if config.DEMO:
        report('--', 'Demo mode (DEMO=1): made-up tracks, mpd is not used')
    else:
        check_mpd(report)

    # ALSA: only a fallback for the audio format
    card = config.ALSA_CARD
    if not os.path.isdir('/proc/asound'):
        report('--', 'ALSA: not visible here (another OS, or a container): mpd\'s format is used')
    elif not os.path.isdir(f'/proc/asound/card{card}'):
        try:
            with open('/proc/asound/cards') as f:
                cards = ', '.join(line.split(']:')[0].strip().replace(' [', ' ').strip()
                                  for line in f if ']:' in line)
        except OSError:
            cards = ''
        report('warn', f'ALSA card {card} not found ({cards or "no card"})',
               'Only used when mpd gives no format; set ALSA_CARD to the right number')
    else:
        try:
            with open(f'/proc/asound/card{card}/id') as f:
                card_id = f.read().strip()
        except OSError:
            card_id = '?'
        fmt = get_alsa_format()
        report('--', f'ALSA card {card} ({card_id}): ' + (f'playing {fmt}' if fmt else 'idle')
               + ' - only used when mpd gives no format')

    # HTTPS
    if config.TLS_CERT or config.TLS_KEY:
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(config.TLS_CERT, config.TLS_KEY or None)
        except (OSError, ssl.SSLError) as e:
            report('FAIL', f'HTTPS: cannot load TLS_CERT / TLS_KEY: {e}', 'Check both paths (relative to this folder)')
        else:
            try:
                cert = ssl._ssl._test_decode_cert(config.TLS_CERT)  # names and expiry; CPython only
            except Exception:
                cert = None
            if not cert:
                report('ok', 'HTTPS: certificate and key load')
            else:
                names = ', '.join(v for _, v in cert.get('subjectAltName', ())) or '?'
                days = (ssl.cert_time_to_seconds(cert['notAfter']) - time.time()) / 86400
                text = f'HTTPS: certificate for {names}, ' + (f'valid {int(days)} more days' if days >= 1
                                                                       else 'expires within a day')
                if days < 0:
                    report('FAIL', f'HTTPS: certificate for {names} expired', 'Make a new one (e.g. with mkcert)')
                else:
                    report('warn' if days < 30 else 'ok', text, 'Renew it soon' if days < 30 else '')
    else:
        report('--', 'HTTPS: off, so phones and tablets get no Wake Lock', 'See "HTTPS" in the README')

    # AirPlay
    pipe = config.SHAIRPORT_PIPE
    if config.DEMO or not pipe:
        report('--', 'AirPlay: off' + (' (demo mode)' if config.DEMO else ' (SHAIRPORT_PIPE is empty)'))
    elif not os.path.exists(pipe):
        report('--', f'AirPlay: no shairport-sync metadata pipe at {pipe}',
               'To show AirPlay too, enable "metadata" in shairport-sync.conf (see the README)')
    elif not status.is_fifo(pipe):
        report('FAIL', f'AirPlay: {pipe} is not a named pipe', 'Check pipe_name in shairport-sync.conf')
    elif not os.access(pipe, os.R_OK):
        report('FAIL', f'AirPlay: no permission to read {pipe}', 'The bridge must be able to read the pipe')
    else:
        report('ok', f'AirPlay: shairport-sync metadata pipe at {pipe}')

    check_spotify(report)
    check_bluetooth(report)

    # Settings page
    if not config.SETTINGS_PAGE:
        report('--', 'Settings page: off (SETTINGS_PAGE=0)')
    elif os.access(config.DATA_DIR, os.W_OK):
        report('ok', f'Settings page at /settings, saved in {config.DATA_DIR}')
    else:
        report('FAIL', f'Settings page: cannot save in {config.DATA_DIR}',
               'Set DATA_DIR to a folder the bridge can write to')

    # Listening history, scrobbling
    if not config.HISTORY_ENABLED:
        report('--', 'Listening history: off (HISTORY=0)')
    else:
        kept = len(history.Listening(status.history_file()).entries)
        report('ok', f'Listening history at /history: {kept} track(s) in {status.history_file()}')
    services = status.scrobble_services()
    for service in services:
        try:
            report('ok', f'{service.name}: scrobbling as {service.user()} '
                         f'({", ".join(config.SCROBBLE_SOURCES) or "nothing"})')
        except history.ScrobbleError as e:
            report('FAIL', f'{service.name}: {e}', 'Check its settings in .env (see "Listening history" in the README)')
        except Exception as e:
            report('warn', f'{service.name} unreachable: {e}')
    if config.LASTFM_SECRET and not config.LASTFM_SESSION:
        report('warn', 'Last.fm: an API secret but no session key, so no scrobbling',
               'Run python3 mpd-bridge.py --lastfm-login')
    elif not services:
        report('--', 'Scrobbling: off (ListenBrainz or Last.fm, see "Listening history" in the README)')

    # VU meters
    fifo = config.MPD_FIFO
    if config.DEMO:
        report('--', 'VU meters (?vu=1): made-up levels (demo mode)')
    elif not fifo:
        report('--', 'VU meters: off (MPD_FIFO is empty)')
    elif not os.path.exists(fifo):
        report('--', f'VU meters (?vu=1): no mpd fifo output at {fifo}',
               'To use them, add a "fifo" audio_output to mpd.conf (see the README)')
    elif not status.is_fifo(fifo):
        report('FAIL', f'VU meters: {fifo} is not a named pipe', 'Check the path of the fifo output in mpd.conf')
    elif not os.access(fifo, os.R_OK):
        report('FAIL', f'VU meters: no permission to read {fifo}', 'The bridge must be able to read the pipe')
    else:
        report('ok', f'VU meters (?vu=1): mpd\'s fifo output at {fifo}')

    # Port
    port = config.PORT
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('0.0.0.0', port))
        report('ok', f'Port {port} is free')
    except OSError:
        if config.TLS_CERT or config.TLS_KEY:
            report('--', f'Port {port} is in use, probably by the bridge itself')
        else:
            try:
                answer = fetch_json(f'http://127.0.0.1:{port}/now')
                ours = 'state' in answer or 'error' in answer
            except Exception:
                ours = False
            if ours:
                report('--', f'Port {port}: the bridge is already running there')
            else:
                report('FAIL', f'Port {port} is taken by another program', 'Choose another PORT in .env')

    # Online artwork
    if config.ITUNES_ENABLED:
        try:
            found = fetch_json('https://itunes.apple.com/search?term=Daft+Punk+One+More+Time'
                               '&media=music&entity=song&limit=1').get('resultCount', 0)
            report('ok' if found else 'warn', 'iTunes Search API: reachable (artwork for radios, and tracks '
                   'mpd has none for)' if found else 'iTunes Search API: no answer to a test search')
        except Exception as e:
            report('warn', f'iTunes Search API unreachable: {e}',
                   'Radios and tracks without a cover will show no artwork (ITUNES_ARTWORK=0 silences this)')
    else:
        report('--', 'iTunes artwork: off (ITUNES_ARTWORK=0)')
    if config.LASTFM_ENABLED:
        try:
            q = urllib.parse.urlencode({'method': 'album.getinfo', 'api_key': config.LASTFM_KEY,
                                        'artist': 'Daft Punk', 'album': 'Discovery', 'format': 'json'})
            answer = fetch_json(f'https://ws.audioscrobbler.com/2.0/?{q}')
            if 'error' in answer:
                report('FAIL', f'Last.fm refused the key: {answer.get("message", answer["error"])}',
                       'Check LASTFM_API_KEY (https://www.last.fm/api/accounts)')
            else:
                report('ok', 'Last.fm: key accepted (artwork fallback)')
        except Exception as e:
            report('warn', f'Last.fm unreachable: {e}')
    else:
        report('--', 'Last.fm artwork fallback: off (no LASTFM_API_KEY)')

    check_version(report)

    print('\n' + ('No problem found.' if not problems else f'{problems} problem(s) to fix.'))
    return 1 if problems else 0


def check_version(report):
    if not config.UPDATE_CHECK:
        report('--', f'Version {VERSION} (update check off: UPDATE_CHECK=0)')
        return
    try:
        latest = updates.fetch(timeout=8)
    except Exception as e:
        report('--', f'Version {VERSION} (cannot ask GitHub for the latest: {e})')
        return
    if updates.newer(latest['version']):
        report('warn', f'Version {VERSION}: {latest["version"]} is out', f'What\'s new, and how to update: {latest["url"]}')
    else:
        report('ok', f'Version {VERSION}, the latest')


def check_bluetooth(report):
    if config.DEMO or not config.BLUETOOTH:
        report('--', 'Bluetooth: off' + (' (demo mode)' if config.DEMO else ' (BLUETOOTH=0)'))
        return
    try:
        objects = bluetooth.managed_objects()
    except bluetooth.BusError as e:
        kind = bluetooth.problem(e)
        if kind == 'denied':
            report('FAIL', f'Bluetooth: BlueZ refused to answer ({e})',
                   'Add the user running the bridge to the "bluetooth" group (sudo usermod -aG bluetooth '
                   'USER), then restart the bridge')
        else:
            report('--', f'Bluetooth: cannot ask BlueZ ({e})',
                   'To show what a phone plays over Bluetooth, see "Bluetooth" in the README')
        return
    devices = [interfaces['org.bluez.Device1'] for interfaces in objects.values() if 'org.bluez.Device1' in interfaces]
    connected = [device.get('Alias') or device.get('Address', '?') for device in devices if device.get('Connected')]
    found = bluetooth.playing(objects)
    if found:
        what = ' - '.join(filter(None, (found['artist'], found['title']))) or 'sound, without what it plays'
        report('ok', f'Bluetooth: {found["sender"] or "a device"} {"plays" if found["state"] == "play" else "paused"} '
               f'{what}')
    else:
        report('ok', 'Bluetooth: BlueZ answers, ' + (f'connected: {", ".join(connected)}' if connected
                                                     else 'no device connected'))


def check_spotify(report):
    # Does raspotify run the event program? (librespot started another way
    # is given --onevent itself: not checked)
    conf = RASPOTIFY_CONF
    if config.DEMO or not config.SPOTIFY_ENABLED:
        report('--', 'Spotify Connect: off' + (' (demo mode)' if config.DEMO else ' (SPOTIFY=0)'))
        return
    try:
        with open(conf) as f:
            text = f.read()
    except FileNotFoundError:
        report('--', 'Spotify Connect: raspotify not found', 'To show Spotify too, see "Spotify Connect" in the README')
        return
    except OSError as e:
        report('--', f'Spotify Connect: cannot read {conf} ({e.strerror})')
        return
    m = re.search(r'^[ \t]*LIBRESPOT_ONEVENT[ \t]*=[ \t]*["\']?([^"\'\n]*)', text, re.M)
    program = m.group(1).split()[0] if m and m.group(1).split() else ''
    if not program:
        report('warn', 'Spotify Connect: raspotify does not tell the bridge what it plays',
               f'Set LIBRESPOT_ONEVENT in {conf} (see "Spotify Connect" in the README)')
    elif not os.access(program, os.X_OK):
        report('FAIL', f'Spotify Connect: LIBRESPOT_ONEVENT runs {program}, which is not an executable file',
               'Install it as the README says (sudo install -m 755 ...)')
    else:
        report('ok', f'Spotify Connect: raspotify tells the bridge what it plays ({program})')


def check_mpd(report):
    where = f'{config.MPD_HOST}:{config.MPD_PORT}'
    try:
        with socket.create_connection((config.MPD_HOST, config.MPD_PORT), timeout=3) as s:
            banner = s.recv(1024).decode('utf-8', 'replace').strip()
    except ConnectionRefusedError:
        report('FAIL', f'mpd: nothing answers at {where}',
               'Is mpd running? Check MPD_HOST / MPD_PORT, and port / bind_to_address in mpd.conf')
        return
    except OSError as e:
        report('FAIL', f'mpd: cannot reach {where}: {e}', 'Check MPD_HOST / MPD_PORT, and any firewall')
        return
    if not banner.startswith('OK MPD'):
        report('FAIL', f'Something else than mpd answers at {where}: {banner[:60]!r}', 'Check MPD_PORT')
        return
    report('ok', f'mpd {banner[7:]} at {where}')
    try:
        mpd_status = mpd.parse(mpd.command('status'))
        song = mpd.parse(mpd.command('currentsong'))
    except mpd.MPDError as e:
        if e.code == mpd.ACK_PASSWORD:
            report('FAIL', 'mpd refused MPD_PASSWORD', 'Compare it with the password line of mpd.conf')
        elif e.code == mpd.ACK_PERMISSION:
            report('FAIL', 'mpd needs a password', 'Set MPD_PASSWORD in .env')
        else:
            report('FAIL', f'mpd answered with an error: {e}')
        return
    except OSError as e:
        report('FAIL', f'mpd stopped answering: {e}')
        return
    report('ok', 'mpd accepts the password' if config.MPD_PASSWORD else 'mpd answers without a password')

    # Instant updates rely on mpd's "idle" command
    try:
        s = mpd.connect()
        try:
            s.sendall(b'idle player\nnoidle\n')
            idle_ok = not mpd.recv(s).startswith('ACK ')
        finally:
            s.close()
        report('ok' if idle_ok else 'warn', 'Instant updates: mpd\'s idle command works' if idle_ok
               else 'mpd refused "idle": the page will poll every 2 s instead')
    except (OSError, mpd.MPDError) as e:
        report('warn', f'Instant updates: idle failed ({e})', 'The page will poll every 2 s instead')

    file_url = song.get('file', '')
    state = mpd_status.get('state', 'stop')
    if state == 'stop' or not file_url:
        report('--', 'Nothing playing: start a track, then check again to see its format and artwork')
        return
    report('ok', f'{"Playing" if state == "play" else "Paused"}: {display_title(song)} ({file_url})')
    audio = mpd_status.get('audio', '')
    fmt = get_audio_format(mpd_status)
    codec, lossless = get_codec(file_url, audio)
    source = 'from mpd' if fmt and fmt == audio else f'from ALSA card {config.ALSA_CARD}' if fmt else ''
    badges = ', '.join(describe_badges(fmt, codec, lossless))
    report('ok' if fmt else 'warn', f'Audio format {fmt} {source} -> badges: {badges or "none"}' if fmt
           else 'No audio format from mpd nor ALSA: no quality badge')
    if '://' not in file_url:  # streams: no artwork in mpd
        for cmd, where_from in (('readpicture', 'embedded in the file'), ('albumart', 'cover file in its folder')):
            try:
                data, mime = mpd.fetch_binary(cmd, file_url)
            except (OSError, mpd.MPDError, ValueError):
                data = None
            if data:
                size = f'{len(data) // 1024} KB' if len(data) >= 1024 else f'{len(data)} bytes'
                report('ok', f'Artwork: {where_from} ({mime or mpd.sniff_mime(data)}, {size})')
                return
        report('--', 'No artwork in mpd for this track (embedded picture or cover file)')
    if not (config.ITUNES_ENABLED or config.LASTFM_ENABLED):
        report('--', 'Online artwork: off', 'ITUNES_ARTWORK=1 (or a LASTFM_API_KEY) looks for it online')
        return
    # What the page gets online: the same lookups, waited for
    try:
        deadline = time.time() + 15
        art = status.get_status()['art_url']
        while not art and artwork.looking() and time.time() < deadline:
            time.sleep(0.2)
            art = status.get_status()['art_url']
    except (OSError, mpd.MPDError) as e:
        report('warn', f'mpd stopped answering: {e}')
        return
    if art:
        report('ok', f'Artwork: found online ({urllib.parse.urlparse(art).netloc})')
    else:
        report('--', 'No artwork online either for this track')


def lastfm_login():
    # Lets the bridge scrobble to your Last.fm account (Last.fm's desktop
    # authentication: a token you allow on its site, exchanged for a session
    # key that doesn't expire)
    if not (config.LASTFM_ENABLED and config.LASTFM_SECRET):
        print('First set LASTFM_API_KEY and LASTFM_API_SECRET in .env: create them (free) at\n'
              'https://www.last.fm/api/account/create')
        return 1
    service = history.LastFm(config.LASTFM_KEY, config.LASTFM_SECRET)
    try:
        token = service.call('auth.getToken')['token']
        print('Open this address, log in to Last.fm if needed, and allow nowplaying:\n\n'
              f'  https://www.last.fm/api/auth/?api_key={config.LASTFM_KEY}&token={token}\n')
        input('Then press Enter here... ')
        session = service.call('auth.getSession', token=token)['session']
    except (history.ScrobbleError, OSError, KeyError) as e:
        print(f'\nLast.fm said no: {e}')
        return 1
    line = f'LASTFM_SESSION_KEY={session["key"]}'
    print(f'\nAllowed for {session["name"]}. The session key, for .env:\n\n  {line}\n')
    env_file = os.path.join(config.SCRIPT_DIR, '.env')
    if input(f'Add it to {env_file} now? [Y/n] ').strip().lower() in ('', 'y', 'yes'):
        try:
            with open(env_file, 'a') as f:
                f.write(f'\n{line}\n')
        except OSError as e:  # e.g. read-only, in a container
            print(f'Cannot write to {env_file} ({e.strerror}): add the line yourself.')
            return 1
        print('Added: restart the bridge to start scrobbling.')
    return 0
