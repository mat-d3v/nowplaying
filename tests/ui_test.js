// Page tests in Chromium: the real bridge and page, against tests/fake_mpd.py.
//
//   npm install --no-save playwright && npx playwright install chromium
//   node tests/ui_test.js
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.dirname(__dirname);
const MPD_PORT = 16600, BRIDGE_PORT = 18766, BASE = `http://localhost:${BRIDGE_PORT}`;
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
  const mpd = start('tests/fake_mpd.py', ['--port', String(MPD_PORT), '--scenario', 'flac_hires'], {});
  const bridge = start('mpd-bridge.py', [], {
    MPD_HOST: '127.0.0.1', MPD_PORT: String(MPD_PORT), PORT: String(BRIDGE_PORT), MPD_PASSWORD: '',
    LASTFM_API_KEY: '', ITUNES_ARTWORK: '0', TLS_CERT: '', TLS_KEY: '',
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

    mpd.kill();
    check('mpd down: explained on the page', await waitFor(nothing, 'MPD unreachable'), 'MPD unreachable');

    check('no JavaScript errors', pageErrors, []);
  } finally {
    await browser.close();
    bridge.kill();
    mpd.kill();
  }
  if (failures.length) {
    console.log(`\n${failures.length} failed`);
    process.exit(1);
  }
  console.log('\nall passed');
})().catch(e => { console.error(e); process.exit(1); });
