"""The display settings saved from the settings page, for every screen.

The same names and values as the page's own address options, which still
win over them, screen by screen. Kept in DATA_DIR/settings.json.
"""
import json
import os

from . import config

CHOICES = {'lang': ('', 'en', 'fr', 'de', 'es', 'it', 'nl'), 'clock': ('24', '12', '0'), 'bg': ('glow', 'blur'),
           'next': ('1', '0'), 'vu': ('0', '1'), 'shift': ('1', '0')}


def path():
    return os.path.join(config.DATA_DIR, 'settings.json')


def valid(settings):
    # The display settings, checked (known names, allowed values), or None
    if not isinstance(settings, dict):
        return None
    clean = {}
    for key, value in settings.items():
        if not isinstance(value, str):
            return None
        if key == 'scale':  # '' fits the screen, else 0.5 to 3
            try:
                scale = float(value) if value else None
            except ValueError:
                return None
            if scale is not None and not 0.5 <= scale <= 3:
                return None
            clean[key] = f'{scale:.1f}' if scale else ''
        elif value in CHOICES.get(key, ()):
            clean[key] = value
        else:
            return None
    return clean


def load():
    try:
        with open(path()) as f:
            return valid(json.load(f)) or {}
    except (OSError, ValueError):
        return {}


def save(settings):
    temporary = path() + '.tmp'
    with open(temporary, 'w') as f:
        json.dump(settings, f, indent=2)
    os.replace(temporary, path())  # never half a file
