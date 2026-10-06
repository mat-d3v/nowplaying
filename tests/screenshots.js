// Retakes the README's screenshots, in English and French, from the bridge
// in demo mode: made-up tracks, their artwork and a made-up history.
//
//   npm install --no-save playwright && npx playwright install chromium
//   node tests/screenshots.js
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = path.dirname(__dirname);
const OUT = path.join(ROOT, 'assets', 'screenshots');
const PORT = 18767, BASE = `http://localhost:${PORT}`;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// As if it were about 9 pm, whenever it runs: the clock shows an evening,
// and the history's "today" holds one
const OFFSET = ((21 - new Date().getUTCHours() + 36) % 24) - 12;  // hours from UTC, -12 to 11
const TIMEZONE = OFFSET ? `Etc/GMT${OFFSET > 0 ? '-' : '+'}${Math.abs(OFFSET)}` : 'Etc/GMT';

(async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'nowplaying-shots-'));
  // One track the whole time (the first, Hi-Res), nothing else running
  const bridge = spawn('python3', [path.join(ROOT, 'mpd-bridge.py')], {
    env: { ...process.env, DEMO: '1', DEMO_STEP: '3600', PORT: String(PORT), DATA_DIR: data, UPDATE_CHECK: '0',
      BLUETOOTH: '0', MQTT_HOST: '', TLS_CERT: '', TLS_KEY: '' },
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  try {
    await sleep(2000);
    for (const [locale, suffix] of [['en-US', ''], ['fr-FR', '.fr']]) {
      const shot = async (name, url, viewport, scale, wait) => {
        const page = await browser.newPage({ viewport, deviceScaleFactor: scale, locale, timezoneId: TIMEZONE });
        await page.goto(BASE + url);
        await sleep(wait);
        await page.screenshot({ path: path.join(OUT, `${name}${suffix}.jpg`), type: 'jpeg', quality: 82 });
        await page.close();
      };
      // The VU meters, their needles moving with the demo's levels
      await shot('vu', '/?vu=1&shift=0', { width: 1480, height: 750 }, 1, 4000);
      // An upright screen (the Raspberry Pi Touch Display 2), and a phone,
      // with the same 9:16 shape side by side in the README
      await shot('upright', '/?shift=0', { width: 720, height: 1280 }, 1, 3000);
      await shot('settings', '/settings', { width: 390, height: 693 }, 2, 3000);
      await shot('history', '/history', { width: 390, height: 693 }, 2, 3000);
      console.log(`${locale}: done`);
    }
  } finally {
    await browser.close();
    bridge.kill();
    fs.rmSync(data, { recursive: true, force: true });
  }
})().catch(e => { console.error(e); process.exit(1); });
