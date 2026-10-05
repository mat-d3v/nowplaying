"""Is a newer version out? Once a day the bridge asks GitHub for its latest
release: the settings page, --check and the log say so. Nothing is sent but
that request. UPDATE_CHECK=0 turns it off.
"""
import json
import logging
import re
import threading
import time
import urllib.request

from . import VERSION, config

log = logging.getLogger('nowplaying')

LATEST = 'https://api.github.com/repos/mat-d3v/nowplaying/releases/latest'
FIRST_CHECK = 60      # seconds after starting: at boot, the network may come up after the bridge
EVERY = 24 * 3600
RETRY = 3600          # after a check that failed

_latest = None        # {'version': '1.2.0', 'url': its release page}, once known
_lock = threading.Lock()


def parse(version):
    # '1.10.0' or 'v1.10.0' -> (1, 10, 0); None if it isn't a version
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', version.strip())
    return tuple(int(n) for n in m.groups()) if m else None


def newer(version, than=VERSION):
    a, b = parse(version), parse(than)
    return bool(a and b and a > b)


def fetch(timeout=10):
    # The latest release: {'version': ..., 'url': ...}
    request = urllib.request.Request(LATEST, headers={'Accept': 'application/vnd.github+json',
                                                      'User-Agent': f'nowplaying/{VERSION}'})
    with urllib.request.urlopen(request, timeout=timeout) as r:
        data = json.loads(r.read())
    version = data['tag_name'].lstrip('v')
    if not parse(version):
        raise ValueError(f'unexpected release name {data["tag_name"]!r}')
    return {'version': version, 'url': data['html_url']}


def available():
    # The newer release, None if this is the latest (or nobody asked)
    with _lock:
        latest = _latest
    return latest if latest and newer(latest['version']) else None


def about():
    # For the settings page (/version)
    return {'version': VERSION, 'update': available(), 'checked': config.UPDATE_CHECK}


def refresh():
    # Asks GitHub now; the newer release, None if this is the latest
    global _latest
    latest = fetch()
    with _lock:
        _latest = latest
    return available()


def watch():
    # The bridge's background check: once a day, each new version logged once
    told = None
    time.sleep(FIRST_CHECK)
    while True:
        try:
            new = refresh()
        except Exception as e:  # no network yet, GitHub's rate limit...
            log.debug('update check failed: %s', e)
            time.sleep(RETRY)
            continue
        if new and new['version'] != told:
            log.info('nowplaying %s is out (this is %s): %s', new['version'], VERSION, new['url'])
            told = new['version']
        time.sleep(EVERY)
