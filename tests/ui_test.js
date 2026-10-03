// Page tests in Chromium: the real bridge and page, against tests/fake_mpd.py.
//
//   npm install --no-save playwright && npx playwright install chromium
//   node tests/ui_test.js
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.dirname(__dirname);
const MPD_PORT = 16600, BRIDGE_PORT = 18766, BASE = `http://localhost:${BRIDGE_PORT}`;
const PIPE = path.join(os.tmpdir(), `nowplaying-airplay-${process.pid}`);  // shairport-sync's metadata pipe
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const failures = [];

function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}${ok ? '' : `: got ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)}`}`);
  if (!ok) failures.push(name);
}

// Switches the fake mpd's scenario, which also wakes the bridge's idle
// connection; quietly: without telling idle clients
function setScenario(name, quiet = false) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(MPD_PORT, '127.0.0.1');
    let answer = '';
    socket.on('connect', () => socket.write(`${quiet ? 'fake-quiet' : 'fake-scenario'} ${name}\n`));
    socket.on('data', data => {
      answer += data;
      if (/^OK$/m.test(answer)) { socket.end(); resolve(); }
    });
    socket.on('error', reject);
  });
}

// What a fake shairport-sync sends over AirPlay ("play", "pause", "stop"...)
function airplay(scenario) {
  execFileSync('python3', [path.join(ROOT, 'tests/fake_shairport.py'), '--pipe', PIPE, scenario]);
}

function start(script, args, env) {
  const proc = spawn('python3', [path.join(ROOT, script), ...args],
    { env: { ...process.env, ...env }, stdio: ['ignore', 'inherit', 'inherit'] });
  return proc;
}

// Polls fn() until it returns expected, or gives up after timeout ms
async function waitFor(fn, expected, timeout = 5000) {
  const until = Date.now() + timeout;
  let value;
  while (Date.now() < until) {
    value = await fn();
    if (JSON.stringify(value) === JSON.stringify(expected)) break;
    await sleep(50);
  }
  return value;
}

(async () => {
  execFileSync('mkfifo', [PIPE]);
  const mpd = start('tests/fake_mpd.py', ['--port', String(MPD_PORT), '--scenario', 'flac_hires'], {});
  const bridge = start('mpd-bridge.py', [], {
    MPD_HOST: '127.0.0.1', MPD_PORT: String(MPD_PORT), PORT: String(BRIDGE_PORT), MPD_PASSWORD: '',
    LASTFM_API_KEY: '', ITUNES_ARTWORK: '0', TLS_CERT: '', TLS_KEY: '', SHAIRPORT_PIPE: PIPE,
  });
  const browser = await chromium.launch();
  try {
    await sleep(1500);
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, locale: 'en-US' });
    const pageErrors = [];
    page.on('pageerror', e => pageErrors.push(String(e)));

    const view = () => page.evaluate(() => {
      const $ = id => document.getElementById(id);
      const shown = id => getComputedStyle($(id)).display !== 'none';
      return {
        title: $('title').textContent, artist: $('artist').textContent, album: $('album').textContent,
        codec: shown('badge-format') ? $('badge-format').textContent : null,
        quality: shown('badge-quality') ? $('badge-quality').textContent : null,
        hires: shown('hires-badge'), live: shown('badge-live'), progress: shown('progress-bar'),
        next: shown('next-track') ? $('next-track-text').textContent : null,
      };
    });
    const title = () => page.evaluate(() => document.getElementById('title').textContent);

    await page.goto(BASE + '/');
    await waitFor(title, 'Song');
    check('FLAC 24/96 badges', await view(), {
      title: 'Song', artist: 'Art', album: 'Alb', codec: 'FLAC', quality: '24bit / 96.0 kHz',
      hires: true, live: false, progress: true, next: null,
    });

    // Pushed by the bridge as soon as mpd reports the change
    const changed = Date.now();
    await setScenario('mp3_mad');
    check('track change pushed (shown within 1.5 s)', await waitFor(title, 'Song MP3', 1500), 'Song MP3');
    console.log(`     (${Date.now() - changed} ms)`);
    const mp3 = await view();
    check('MP3: codec, no bit depth, no Hi-Res', [mp3.codec, mp3.quality, mp3.hires], ['MP3', '44.1 kHz', false]);

    await setScenario('dsf');
    await waitFor(title, 'Song DSD');
    const dsd = await view();
    check('DSD badges', [dsd.codec, dsd.quality, dsd.hires], ['DSF', 'DSD64', true]);

    await setScenario('upnp_flac');
    await waitFor(title, 'Pier');
    const upnp = await view();
    check('UPnP track (http address): FLAC badges, Hi-Res, progress, no Live badge',
      [upnp.codec, upnp.quality, upnp.hires, upnp.live, upnp.progress], ['FLAC', '24bit / 192.0 kHz', true, false, true]);

    await setScenario('untagged');
    await waitFor(title, 'track01');
    const untagged = await view();
    check('untagged file: file name and next track', [untagged.title, untagged.next], ['track01', 'track02']);

    await setScenario('radio_artist_title');
    await waitFor(title, 'One More Time');
    check('radio: split title, live badge, no progress bar', await view(), {
      title: 'One More Time', artist: 'Daft Punk', album: 'Radio X', codec: null, quality: '44.1 kHz',
      hires: false, live: true, progress: false, next: null,
    });

    // A passing error only the health-check poll sees (no idle event), then
    // mpd answering again: the poll must clear the error screen by itself.
    // That poll runs every 10 s, on a 2 s tick: allow up to 15 s
    const screen = () => page.evaluate(() => getComputedStyle(document.getElementById('nothing')).display !== 'none'
      ? document.getElementById('nothing-title').textContent : document.getElementById('title').textContent);
    await setScenario('bad_status', true);
    check('passing error shown by the health check', await waitFor(screen, 'MPD error', 15000), 'MPD error');
    await setScenario('radio_artist_title', true);
    check('...and cleared once mpd answers again', await waitFor(screen, 'One More Time', 15000), 'One More Time');

    await setScenario('stopped');
    const nothing = () => page.evaluate(() => getComputedStyle(document.getElementById('nothing')).display !== 'none'
      && document.getElementById('nothing-title').textContent);
    check('stopped: nothing playing', await waitFor(nothing, 'Nothing playing'), 'Nothing playing');

    const fr = await browser.newPage({ locale: 'fr-FR' });
    await fr.goto(BASE + '/');
    await sleep(800);
    check('French from the browser language', await fr.evaluate(() =>
      [document.documentElement.lang, document.getElementById('nothing-title').textContent]),
    ['fr', 'Aucune lecture en cours']);
    await fr.goto(BASE + '/?lang=en');
    await sleep(800);
    check('?lang=en wins over the browser', await fr.evaluate(() => document.documentElement.lang), 'en');

    // Burn-in protection: one shift moves what's drawn by a few pixels
    await setScenario('flac_hires');
    await waitFor(title, 'Song');
    await page.evaluate(() => shiftScreen());
    await sleep(4500);  // the 4 s transition
    check('burn-in protection shifts the content and the clock', await page.evaluate(() =>
      ['content', 'clock'].map(id => getComputedStyle(document.getElementById(id)).transform)),
    ['matrix(1, 0, 0, 1, 4, -3)', 'matrix(1, 0, 0, 1, 4, -3)']);

    // ?scale: everything bigger, still within the screen; junk ignored, capped at 3
    const scaled = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    for (const [query, width] of [['?scale=1.5', 570], ['?scale=oops', 380], ['?scale=10', 1140]]) {
      await scaled.goto(BASE + '/' + query);
      await sleep(2000);  // artwork loaded, its fade-in done
      check(`${query}: artwork ${width} px wide, no sideways overflow`, await scaled.evaluate(() => [
        Math.round(document.getElementById('artwork').getBoundingClientRect().width),
        document.documentElement.scrollWidth <= document.documentElement.clientWidth]), [width, true]);
    }

    // Any screen: upright ones stack the artwork over the text, small ones
    // shrink the layout (the text keeps its room), big ones grow it
    const layoutOn = async (width, height) => {
      const sized = await browser.newPage({ viewport: { width, height } });
      await sized.goto(BASE + '/');
      await sleep(1500);
      const layout = await sized.evaluate(() => {
        const art = document.getElementById('artwork').getBoundingClientRect();
        const info = document.getElementById('info').getBoundingClientRect();
        return [Math.round(art.width), art.bottom <= info.top ? 'column' : 'row',
          Math.min(art.top, info.top) >= 0 && Math.max(art.bottom, info.bottom) <= innerHeight
            && Math.max(art.right, info.right) <= innerWidth];
      });
      await sized.close();
      return layout;
    };
    for (const [width, height, art, layout] of [[720, 1280, 560, 'column'], [1080, 1920, 840, 'column'],
      [390, 844, 242, 'column'], [800, 480, 276, 'row'], [3840, 2160, 760, 'row']]) {
      check(`${width}x${height}: ${layout}, artwork ${art} px, all on screen`, await layoutOn(width, height),
        [art, layout, true]);
    }

    // AirPlay (shairport-sync) takes over while it plays, then mpd comes back
    airplay('play');
    check('AirPlay shown over mpd', await waitFor(title, 'Harbor Lights', 3000), 'Harbor Lights');
    const airplayView = () => page.evaluate(() => [
      document.getElementById('badge-format').textContent,
      document.getElementById('status-text').textContent,
      document.getElementById('artwork').src.includes('/art?airplay=') && document.getElementById('artwork').naturalWidth]);
    check('AirPlay: badge, sender, cover', await waitFor(airplayView, ['AirPlay', "Playing · Mat's iPhone", 64]),
      ['AirPlay', "Playing · Mat's iPhone", 64]);
    airplay('stop');
    check('mpd back when AirPlay stops', await waitFor(title, 'Song', 3000), 'Song');

    // Spotify Connect: librespot runs spotify-event.py on each event
    const spotify = event => execFileSync('python3', [path.join(ROOT, 'spotify-event.py'), BASE],
      { env: { ...process.env, ...event } });
    spotify({ PLAYER_EVENT: 'session_client_changed', CLIENT_NAME: 'Salon' });
    spotify({ PLAYER_EVENT: 'track_changed', NAME: 'Blue Hour', ARTISTS: 'Vela Nova', ALBUM: 'City After Hours',
      DURATION_MS: '240000', COVERS: '' });
    spotify({ PLAYER_EVENT: 'playing', POSITION_MS: '15000' });
    check('Spotify shown over mpd', await waitFor(title, 'Blue Hour', 3000), 'Blue Hour');
    check('Spotify: badge and sender', await page.evaluate(() => [
      document.getElementById('badge-format').textContent, document.getElementById('status-text').textContent]),
    ['Spotify', 'Playing · Salon']);
    spotify({ PLAYER_EVENT: 'stopped' });
    check('mpd back when Spotify stops', await waitFor(title, 'Song', 3000), 'Song');

    mpd.kill();
    check('mpd down: explained on the page', await waitFor(nothing, 'MPD unreachable'), 'MPD unreachable');

    check('no JavaScript errors', pageErrors, []);
  } finally {
    await browser.close();
    bridge.kill();
    mpd.kill();
    fs.unlinkSync(PIPE);
  }
  if (failures.length) {
    console.log(`\n${failures.length} failed`);
    process.exit(1);
  }
  console.log('\nall passed');
})().catch(e => { console.error(e); process.exit(1); });
