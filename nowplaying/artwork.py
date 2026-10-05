"""Artwork from the Internet, for what mpd has none for, and radios.

Last.fm (with an API key) by album, then the iTunes Search API (free, no
key) by album or song. Lookups run in the background: the title shows at
once, the artwork follows, and changed() lets the pages know.
"""
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger('nowplaying')


def changed():
    # Called when a lookup is done: status.py makes it push the status again
    pass


def lastfm_art(artist, album):
    # Last.fm's artwork for an album (needs LASTFM_API_KEY), '' if it has none
    q = urllib.parse.urlencode({'method': 'album.getinfo', 'api_key': config.LASTFM_KEY, 'artist': artist,
                                'album': album, 'format': 'json'})
    with urllib.request.urlopen(f'https://ws.audioscrobbler.com/2.0/?{q}', timeout=5) as r:
        data = json.loads(r.read())
    for img in reversed(data.get('album', {}).get('image', [])):
        if img.get('#text'):
            return re.sub(r'/\d+x\d+/', '/', img['#text'])
    return ''


def simplify(name):
    # Names compared loosely: case, accents, punctuation, and notes such as
    # "(Remastered 2011)", "[Deluxe Edition]" or " - Single" don't count
    bare = re.sub(r'\s*(\([^)]*\)|\[[^\]]*\])', '', name)
    bare = re.sub(r'\s+-\s+(single|ep)\s*$', '', bare, flags=re.I).strip() or name
    bare = ''.join(c for c in unicodedata.normalize('NFKD', bare) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[^\W_]+', bare.lower()))


def names_match(a, b, exact=False):
    # "Daft Punk" matches "Daft Punk feat. Pharrell Williams" (whole words),
    # unless exact
    a, b = simplify(a), simplify(b)
    if not a or not b:
        return False
    return a == b if exact else f' {a} ' in f' {b} ' or f' {b} ' in f' {a} '


def itunes_search(term, entity):
    q = urllib.parse.urlencode({'term': term, 'media': 'music', 'entity': entity, 'limit': 10})
    with urllib.request.urlopen(f'https://itunes.apple.com/search?{q}', timeout=5) as r:
        return json.loads(r.read()).get('results', [])


def itunes_art(artist, title, album='', album_artist=''):
    # Artwork from the iTunes Search API (free, no key): the album when the
    # tags name one, else the song (radios send no album). Only a result
    # whose names match counts: no artwork rather than someone else's
    def pick(results, field, name, loose, by):
        # The same name first ("Discovery" before "Discovery (Live)"), then
        # a looser match, always by the same artist
        results = [r for r in results if names_match(r.get('artistName', ''), by)]
        same = [r for r in results if r.get(field, '').casefold() == name.casefold()]
        close = [r for r in results if names_match(r.get(field, ''), name, exact=not loose)]
        return ((same or close or [{}])[0]).get('artworkUrl100', '')

    found = ''
    if album:
        by = album_artist or artist
        found = pick(itunes_search(f'{by} {album}', 'album'), 'collectionName', album, False, by)
    if not found and title:
        found = pick(itunes_search(f'{artist} {title}', 'song'), 'trackName', title, True, artist)
    # 100x100 thumbnails; the same address serves bigger sizes
    return found.replace('/100x100bb.', '/600x600bb.')


RETRY_AFTER = 300  # seconds before an online lookup that failed (no network yet?) runs again
_found = {}        # lookup -> (artwork address or '', when); None as address: the lookup failed
_running = set()
_lock = threading.Lock()


def online_art(key, lookup):
    # Online artwork, without holding up the page: the address once known
    # ('' when the service has none), None while lookup() runs in the
    # background. When it's done the status is pushed again, and the page
    # loads artwork that arrives after the title
    with _lock:
        known = _found.get(key)
        if known and (known[0] is not None or time.monotonic() - known[1] < RETRY_AFTER):
            return known[0] or ''
        if key in _running:
            return None
        _running.add(key)
    threading.Thread(target=_look_up, args=(key, lookup), daemon=True).start()
    return None


def looking():
    # Whether lookups are still running
    with _lock:
        return bool(_running)


def _look_up(key, lookup):
    try:
        found = lookup()
    except Exception as e:
        log.warning('%s artwork lookup failed for %s: %s', key[0], ' / '.join(filter(None, key[1:])), e)
        found = None
    with _lock:
        _running.discard(key)
        if len(_found) > 500:
            _found.clear()  # simple cap to avoid unbounded growth
        _found[key] = (found, time.monotonic())
    changed()  # with the artwork, or on to the next place to look


def for_track(artist, title, album='', album_artist='', exclude=''):
    # The online artwork for a track: Last.fm (with an API key) by album,
    # then iTunes; '' while looking. Never by the exclude name (a radio's
    # own name, shown where its artist would be)
    looking_up = False
    art = ''
    if artist and album and artist != exclude and config.LASTFM_ENABLED:
        found = online_art(('Last.fm', artist, album), lambda: lastfm_art(artist, album))
        looking_up, art = found is None, found or ''
    if not art and not looking_up and artist and artist != exclude and config.ITUNES_ENABLED:
        # A compilation is under its album artist ("Various Artists")
        key = ('iTunes', artist, title, album, album_artist)
        art = online_art(key, lambda: itunes_art(*key[1:])) or ''
    return art
