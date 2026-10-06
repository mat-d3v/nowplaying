"""Starts the bridge: python3 mpd-bridge.py [--check | --lastfm-login | --version]."""
import logging
import os
import shutil
import ssl
import sys
import threading

from . import VERSION, bluetooth, check, config, history, levels, shairport, spotify, status, updates
from .server import Handler, Server

log = logging.getLogger('nowplaying')


def main():
    # systemd's journal already timestamps each line
    logging.basicConfig(level=logging.INFO, format=('%(levelname)s %(message)s' if os.environ.get('JOURNAL_STREAM')
                                                    else '%(asctime)s %(levelname)s %(message)s'))
    if '--version' in sys.argv[1:]:
        print(f'nowplaying {VERSION}')
        return
    if '--check' in sys.argv[1:]:
        raise SystemExit(check.check())
    if '--lastfm-login' in sys.argv[1:]:
        raise SystemExit(check.lastfm_login())
    server = Server(('0.0.0.0', config.PORT), Handler)
    if config.TLS_CERT or config.TLS_KEY:
        server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            server.tls.load_cert_chain(config.TLS_CERT, config.TLS_KEY or None)
        except (OSError, ssl.SSLError) as e:
            log.error('cannot load TLS_CERT=%s / TLS_KEY=%s: %s', config.TLS_CERT, config.TLS_KEY, e)
            raise SystemExit(1)
    if config.DEMO:
        log.info('nowplaying %s on %s port %s, demo mode: made-up tracks, mpd is not used',
                 VERSION, 'HTTPS' if server.tls else 'HTTP', config.PORT)
    else:
        log.info('nowplaying %s on %s port %s, mpd at %s:%s%s, Last.fm artwork %s, iTunes artwork %s',
                 VERSION, 'HTTPS' if server.tls else 'HTTP', config.PORT, config.MPD_HOST, config.MPD_PORT,
                 ' (with password)' if config.MPD_PASSWORD else '',
                 'on' if config.LASTFM_ENABLED else 'off', 'on' if config.ITUNES_ENABLED else 'off')
    start_players()
    services = [] if config.DEMO else status.scrobble_services()
    if config.HISTORY_ENABLED or services:
        status.LISTENING = history.Listening(
            status.history_file() if config.HISTORY_ENABLED and not config.DEMO else None,
            history.Scrobbler(services, log) if services else None, config.SCROBBLE_SOURCES, log)
        log.info('listening history %s, scrobbling %s',
                 ('off' if not config.HISTORY_ENABLED else 'in memory (demo mode)' if config.DEMO
                  else f'in {status.history_file()}'),
                 f'to {" and ".join(s.name for s in services)} ({", ".join(config.SCROBBLE_SOURCES)})'
                 if services else 'off')
    threading.Thread(target=status.watch_demo if config.DEMO else status.watch_mpd, name='updates',
                     daemon=True).start()
    if status.AIRPLAY:
        threading.Thread(target=shairport.follow, args=(config.SHAIRPORT_PIPE, status.AIRPLAY,
                                                        status.publish_status, log),
                         name='airplay', daemon=True).start()
    if status.BLUETOOTH:
        threading.Thread(target=bluetooth.follow, args=(status.BLUETOOTH, status.publish_status, log),
                         name='bluetooth', daemon=True).start()
    if config.UPDATE_CHECK:
        threading.Thread(target=updates.watch, name='version-check', daemon=True).start()
    server.serve_forever()


def start_players():
    # AirPlay, Spotify Connect and Bluetooth besides mpd (not in the demo
    # mode), and the VU meters' levels
    if not config.DEMO:
        status.AIRPLAY = shairport.AirPlay() if config.SHAIRPORT_PIPE else None
        status.SPOTIFY = spotify.Spotify() if config.SPOTIFY_ENABLED else None
        if config.BLUETOOTH and shutil.which('busctl'):
            status.BLUETOOTH = bluetooth.Bluetooth()
        elif config.BLUETOOTH:
            log.info("Bluetooth: off, busctl (systemd's D-Bus tool) not found")
    status.OTHER_PLAYERS = [player for player in (status.AIRPLAY, status.SPOTIFY, status.BLUETOOTH) if player]
    status.LEVELS = (levels.LevelMeter(levels.demo_levels) if config.DEMO
                     else levels.LevelMeter(levels.fifo_levels(config.MPD_FIFO, log)) if config.MPD_FIFO
                     else None)
