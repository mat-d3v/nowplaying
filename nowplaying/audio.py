"""Audio formats and codecs, and the badges the page makes of them."""
import os
import re
import urllib.parse

from . import config


def get_alsa_format():
    try:
        with open(f'/proc/asound/card{config.ALSA_CARD}/pcm0p/sub0/hw_params') as f:
            content = f.read()
        rate = ''
        bits = ''
        for line in content.splitlines():
            if line.startswith('format:'):
                m = re.search(r'S(\d+)', line)
                if m:
                    bits = m.group(1)
            if line.startswith('rate:'):
                rate = line.split()[1]
        if rate and bits:
            return f"{rate}:{bits}:2"
        return ''
    except OSError:
        return ''  # no such card, or nothing playing on it


def get_audio_format(status):
    # mpd's own "audio" field is the decoded source format ("44100:16:2",
    # "44100:f:2" for float decoders, "dsd64:2"): that's what the badges
    # describe. The ALSA hardware format is only a fallback, since a DAC
    # that takes 32-bit samples would turn every CD rip into "32bit".
    audio = status.get('audio', '')
    if re.fullmatch(r'(\d+:(\d+|f)|dsd\d+):\d+', audio):
        return audio
    return get_alsa_format()


# File extension -> (badge label, lossless)
CODECS = {
    'flac': ('FLAC', True), 'wav': ('WAV', True), 'aif': ('AIFF', True), 'aiff': ('AIFF', True),
    'ape': ('APE', True), 'wv': ('WavPack', True), 'dsf': ('DSF', True), 'dff': ('DFF', True),
    'mp3': ('MP3', False), 'aac': ('AAC', False), 'ogg': ('OGG', False), 'oga': ('OGG', False),
    'opus': ('OPUS', False), 'wma': ('WMA', False), 'mpc': ('MPC', False),
}

# Qobuz's own stream addresses give their format as a number
QOBUZ_FORMATS = {'5': 'mp3', '6': 'flac', '7': 'flac', '27': 'flac'}


def _known(ext):
    return ext in CODECS or ext in ('m4a', 'mp4')


def _extension(file_url):
    # The file's extension. An address (UPnP/DLNA servers and controllers:
    # upmpdcli, BubbleUPnP, MinimServer... give mpd http addresses) usually
    # ends like the file ("/01%20Song.flac?..."); else its parameters may
    # say ("?file=01.flac", "?format=flac", Qobuz's "fmt=27")
    def ext_of(text):
        name = text.rsplit('/', 1)[-1]
        return name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if '://' not in file_url:
        return ext_of(file_url)
    url = urllib.parse.urlparse(file_url)
    params = urllib.parse.parse_qsl(url.query)
    for text in [url.path] + [value for _, value in params]:
        if _known(ext_of(text)):
            return ext_of(text)
    last = url.path.rstrip('/').rsplit('/', 1)[-1].lower()
    if last in CODECS:  # ".../format/flac"
        return last
    for name, value in params:
        if name.lower() in ('format', 'fmt', 'ext', 'codec') and _known(value.lower().lstrip('.')):
            return value.lower().lstrip('.')
    if 'qobuz' in (url.hostname or ''):
        return QOBUZ_FORMATS.get(dict(params).get('fmt', ''), '')
    return ''


def get_codec(file_url, audio):
    # (badge, lossless), from the file's extension
    if not file_url:
        return '', False
    url = '://' in file_url
    ext = _extension(file_url)
    if ext in ('m4a', 'mp4'):
        # Same container for AAC and ALAC: mpd decodes AAC to float samples
        # ("44100:f:2") and ALAC to integer ones ("44100:16:2")
        if not audio:
            return 'M4A', False
        return ('AAC', False) if audio.split(':')[1:2] == ['f'] else ('ALAC', True)
    if ext in CODECS or (ext and not url):
        return CODECS.get(ext, (ext.upper(), False))
    # An address that doesn't say what it carries (a radio, a playlist.m3u8,
    # a stream from a service): no codec badge rather than a wrong one. Its
    # samples can still tell: integers at 88.2 kHz or more only come from
    # lossless files (lossy decoders give floating point, or 48 kHz at most)
    parts = audio.split(':')
    lossless = url and len(parts) == 3 and parts[0].isdigit() and int(parts[0]) >= 88200 and parts[1].isdigit()
    return '', lossless


def describe_badges(fmt, codec, lossless):
    # The badges the page shows for this format (same rules as index.html)
    parts = fmt.split(':')
    badges = [codec] if codec else []
    dsd = re.fullmatch(r'dsd(\d+)', parts[0]) if fmt else None
    if dsd:
        return badges + [f'DSD{dsd.group(1)}', 'Hi-Res']
    if fmt:
        rate = int(parts[0]) if parts[0].isdigit() else 0
        bits = int(parts[1]) if lossless and parts[1:2] and parts[1].isdigit() else 0
        khz = f'{rate / 1000:.1f} kHz' if rate >= 1000 else ''
        badges += [f'{bits}bit / {khz}' if bits and khz else khz] if khz else []
        if lossless and (rate >= 88200 or bits >= 24):
            badges.append('Hi-Res')
    return badges


def display_title(song):
    # Untagged files and radios without a title still get a name instead of
    # "Nothing playing": station name, then file name, then stream host
    title = song.get('title') or song.get('name')
    if title:
        return title
    uri = song.get('file', '')
    if '://' in uri:
        return urllib.parse.urlparse(uri).netloc or uri
    return os.path.splitext(uri.rsplit('/', 1)[-1])[0]
