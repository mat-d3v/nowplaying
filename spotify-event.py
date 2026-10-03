#!/usr/bin/env python3
"""Tells nowplaying what Spotify Connect plays: librespot's --onevent program.

librespot (raspotify) runs this on each event (track changed, playing,
paused...), with the details in environment variables; this posts them to
the bridge on this machine. Install it outside your home folder, where
raspotify's service can run it:

    sudo install -m 755 spotify-event.py /usr/local/bin/nowplaying-spotify-event

then set it in /etc/raspotify/conf, and restart raspotify:

    LIBRESPOT_ONEVENT=/usr/local/bin/nowplaying-spotify-event

The bridge is at http://127.0.0.1:8766 unless given as an argument, e.g.
LIBRESPOT_ONEVENT="/usr/local/bin/nowplaying-spotify-event https://127.0.0.1:8766"
"""
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

FIELDS = ('PLAYER_EVENT', 'TRACK_ID', 'NAME', 'ARTISTS', 'ALBUM', 'COVERS', 'DURATION_MS', 'POSITION_MS',
          'ITEM_TYPE', 'SHOW_NAME', 'CLIENT_NAME')


def main():
    bridge = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8766').rstrip('/')
    event = {key: os.environ[key] for key in FIELDS if key in os.environ}
    if not event.get('PLAYER_EVENT'):
        return
    request = urllib.request.Request(bridge + '/spotify', data=json.dumps(event).encode(),
                                     headers={'Content-Type': 'application/json'})
    handlers = [urllib.request.ProxyHandler({})]  # never through a proxy
    if urllib.parse.urlparse(bridge).hostname in ('127.0.0.1', 'localhost', '::1'):
        # The bridge's certificate names the machine, not 127.0.0.1
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    try:
        urllib.request.build_opener(*handlers).open(request, timeout=3).close()
    except Exception as e:  # the bridge isn't running: librespot carries on
        print(f'nowplaying-spotify-event: {bridge}: {e}', file=sys.stderr)


if __name__ == '__main__':
    main()
