#!/usr/bin/env python3
"""A fake BlueZ, as the bridge sees it through busctl: for the Bluetooth tests.

    python3 tests/fake_bluez.py busctl ARGS...
    python3 tests/fake_bluez.py install FOLDER
    python3 tests/fake_bluez.py FILE playing|paused|nothing

The first answers like "busctl --json=short call org.bluez / ...
GetManagedObjects", with the objects in the JSON file named by
FAKE_BLUEZ, typed the way busctl writes them; a file holding {"error":
"..."} makes the call fail with that message, as busctl does. The second
puts a "busctl" in that folder that runs it, the third writes what the
fake BlueZ says: a phone playing, paused, or nothing connected.
"""
import json
import os
import stat
import sys

PHONE = '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF'
HEADPHONES = '/org/bluez/hci0/dev_11_22_33_44_55_66'
SINK = '0000110b-0000-1000-8000-00805f9b34fb'
SOURCE = '0000110a-0000-1000-8000-00805f9b34fb'


def device(path, alias, uuid, transport='active', player=None):
    # A connected device, its A2DP transport (sink: it streams here;
    # source: this machine streams to it) and its AVRCP player, if any
    objects = {
        path: {'org.bluez.Device1': {'Alias': alias, 'Address': path[-17:].replace('_', ':'), 'Connected': True}},
        path + '/sep1/fd0': {'org.bluez.MediaTransport1': {'Device': path, 'UUID': uuid, 'State': transport,
                                                           'Codec': 2}},
    }
    if player:
        objects[path + '/player0'] = {'org.bluez.MediaPlayer1': dict(player, Device=path)}
    return objects


def phone(status='playing', title='Harbor Lights', artist='June Avenue', album='Night Ferries', duration=200000,
          position=42000, transport='active', alias="Mat's iPhone"):
    player = {'Name': 'Music', 'Status': status, 'Position': position,
              'Track': {'Title': title, 'Artist': artist, 'Album': album, 'Duration': duration, 'TrackNumber': 3}}
    return device(PHONE, alias, SINK, transport, player)


def headphones():
    return device(HEADPHONES, 'Headphones', SOURCE, player={'Status': 'playing', 'Position': 0, 'Track': {}})


def objects(*devices):
    found = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Alias': 'raspberrypi', 'Powered': True}}}
    for each in devices:
        found.update(each)
    return found


def write(path, found=None, error=None):
    # What the fake BlueZ says from now on
    with open(path + '.tmp', 'w') as f:
        json.dump({'error': error} if error else {'objects': found if found is not None else objects()}, f)
    os.replace(path + '.tmp', path)


def install(folder):
    # A "busctl" in that folder, to put first in PATH
    path = os.path.join(folder, 'busctl')
    with open(path, 'w') as f:
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.abspath(__file__)}" busctl "$@"\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def typed(value):
    # busctl's JSON: each value a variant, with its D-Bus type
    if isinstance(value, bool):
        return {'type': 'b', 'data': value}
    if isinstance(value, int):
        return {'type': 'u', 'data': value}
    if isinstance(value, str):
        return {'type': 'o' if value.startswith('/org/') else 's', 'data': value}
    if isinstance(value, dict):
        return {'type': 'a{sv}', 'data': {key: typed(item) for key, item in value.items()}}
    raise TypeError(value)


def busctl():
    if '--json=short' not in sys.argv or 'GetManagedObjects' not in sys.argv:
        print('fake busctl: only GetManagedObjects', file=sys.stderr)
        return 1
    with open(os.environ['FAKE_BLUEZ']) as f:
        state = json.load(f)
    if state.get('error'):
        print(state['error'], file=sys.stderr)
        return 1
    found = {path: {name: {key: typed(value) for key, value in properties.items()}
                    for name, properties in interfaces.items()}
             for path, interfaces in state['objects'].items()}
    print(json.dumps({'type': 'a{oa{sa{sv}}}', 'data': [found]}))
    return 0


if __name__ == '__main__':
    if sys.argv[1] == 'busctl':
        sys.exit(busctl())
    elif sys.argv[1] == 'install':
        install(sys.argv[2])
    else:
        write(sys.argv[1], objects(*{'playing': [phone()], 'paused': [phone('paused')], 'nothing': []}[sys.argv[2]]))
