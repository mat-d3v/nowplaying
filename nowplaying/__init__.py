"""nowplaying: a full-screen "Now Playing" page for mpd, and what plays
through AirPlay, Spotify Connect and Bluetooth, with the bridge serving it.

mpd-bridge.py runs it (see main.py); one module per job:

    config     settings, from the environment and .env
    mpd        mpd's protocol: commands, idle, embedded artwork
    audio      formats, codecs and the badges they make
    artwork    artwork from the Internet (iTunes, Last.fm)
    status     what's playing, whoever plays it; updates for the pages
    display    the display settings of the settings page
    server     the web server: pages, updates, artwork
    check      --check and --lastfm-login
    updates    is a newer version out?
    shairport, spotify, bluetooth, levels, history, demo: AirPlay, Spotify
    Connect, Bluetooth, the VU meters' levels, the listening history, the
    demo mode
"""
VERSION = '1.2.0'  # with a matching section in CHANGELOG.md
