const {test, expect} = require('@playwright/test');

test('dashboard uses real API contracts and preserves edits', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await expect(page.locator('#net-hotspot-status')).toHaveText('Active');
  await expect(page.locator('#net-usb-status')).toHaveText('Phone connected');
  await expect(page.locator('#url-cam1')).toHaveText('rtmp://10.55.0.1:1935/cam1');
  await expect(page.locator('#audio-follow')).toHaveClass(/bg-brand-blue/);
  await page.locator('#home-name').fill('Test FC');
  await page.waitForTimeout(2200);
  await expect(page.locator('#home-name')).toHaveValue('Test FC');
  await page.locator('#away-name').click();
  await expect.poll(async () => (await page.request.get('/api/scoreboard')).json().then(x=>x.scoreboard.home_name)).toBe('Test FC');
  await page.evaluate(() => adjustScore('home',1));
  await expect(page.locator('#home-score-display')).toHaveText('1');
  await page.evaluate(() => clockAction('second_half'));
  await expect(page.locator('#match-clock-display')).toHaveText('45:00');
  await page.evaluate(() => clockAction('start'));
  await expect(page.locator('#match-clock-display')).not.toHaveText('45:00',{timeout:5000});
  await expect(page.locator('#btn-cut-cam2')).toBeDisabled();
  await expect(page.locator('#btn-cut-cam1')).toBeEnabled();
  await page.locator('#btn-cut-cam1').click();
  await expect(page.locator('#toast-container')).toContainText('Camera unavailable');
  await expect(page.locator('#btn-cut-cam1')).not.toHaveClass(/bg-brand-red/);
  await page.locator('#audio-follow').click();
  await expect(page.locator('#audio-follow')).toHaveClass(/bg-brand-blue/);
  expect(errors).toEqual([]);
  await page.screenshot({path:'/tmp/mcc-dashboard.png',fullPage:true});
});

test('mobile layout and unavailable server clear live tally', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.route('**/api/status',route=>route.abort());
  await expect(page.locator('#status-yt-badge')).toHaveText('UNKNOWN',{timeout:10000});
});

test('USB microphone picker stays open across status polls', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.locator('#audio-usb').click();
  await expect(page.locator('#usb-audio-selector')).toBeVisible();
  await page.waitForTimeout(4500);
  await expect(page.locator('#usb-audio-selector')).toBeVisible();
  await expect(page.locator('#usb-audio-device option[value="alsa_input.usb-test"]')).toHaveCount(1);
  await page.locator('#audio-follow').click();
  await expect(page.locator('#usb-audio-selector')).toBeHidden();
});

test('match mode puts sideline controls on one phone screen', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await expect(page.locator('#match-panel')).toBeHidden();
  await expect(page.locator('iframe[data-preview="setup"]').first()).toHaveAttribute('src', /:8889\/cam1$/);
  await page.locator('#mode-toggle').click();
  await expect(page.locator('#match-panel')).toBeVisible();
  await expect(page.locator('#hotspot-ssid')).toBeHidden();
  // Hidden setup previews stop playing; only the program preview loads.
  await expect(page.locator('iframe[src]')).toHaveCount(1);
  await expect(page.locator('iframe[data-preview="match"]')).toHaveAttribute('src', /:8889\/program$/);
  await page.locator('#match-preview-toggle').click();
  await expect(page.locator('iframe[src]')).toHaveCount(0);
  // Without the preview, cuts, audio, score, clock and stream all fit above the fold.
  const box = await page.locator('#match-panel').boundingBox();
  expect(box.y + box.height).toBeLessThanOrEqual(844);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  const panel = page.locator('#match-panel');
  const before = Number(await panel.locator('[data-bind="away_score"]').textContent());
  await panel.getByLabel('away goal').click();
  await expect(panel.locator('[data-bind="away_score"]')).toHaveText(String(before + 1));
  await expect(page.locator('#toast-container')).toBeEmpty();
  await page.locator('#match-clock-toggle').click();
  await expect(page.locator('#match-clock-toggle')).toHaveText('⏸ Pause');
  await page.locator('#match-clock-toggle').click();
  await expect(page.locator('#match-clock-toggle')).toHaveText('▶ Start');
  await expect(panel.locator('[data-cut="cam1"] [data-cut-state]')).toHaveText('Ready');
  await expect(panel.locator('[data-cut="cam3"]')).toBeDisabled();
  const cut = page.waitForRequest('**/api/switch/cam1');
  await page.keyboard.press('1');
  await cut;
  await page.reload();
  await expect(page.locator('#match-panel')).toBeVisible();
  await expect(page.locator('#match-preview')).toBeHidden();
  await page.screenshot({path:'/tmp/mcc-match-mode.png'});
  expect(errors).toEqual([]);
});

test('laptop on Wi-Fi without tether or hotspot gives cameras the Wi-Fi address', async ({page}) => {
  await page.route('**/api/status', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.network = {internet_ok: true, hotspot_active: false, wifi_ip: 'N/A', usb_iface: null,
      usb_status: 'Disconnected', camera_mode: 'lan', camera_ip: '192.168.1.50', lan_type: 'wifi',
      lan_iface: 'wlan0', lan_name: 'Club WiFi', lan_ip: '192.168.1.50', uplink: 'Club WiFi'};
    body.firewall = {status: 'warning', label: 'ufw firewall on', summary: 'The ufw firewall blocks incoming connections by default.',
      commands: ['sudo ufw allow in on wlan0 to any port 5000,1935,8888,8889 proto tcp'], details: '5000/tcp: listening'};
    await route.fulfill({response, json: body});
  });
  await page.goto('/');
  await expect(page.locator('#url-cam1')).toHaveText('rtmp://192.168.1.50:1935/cam1');
  await expect(page.locator('#camera-network-title')).toContainText('same Wi-Fi “Club WiFi”');
  await expect(page.locator('#status-hotspot-ip')).toHaveText('192.168.1.50');
  await expect(page.locator('#status-usb-label')).toHaveText('Club WiFi');
  await expect(page.locator('#hotspot-note')).toBeVisible();
  await expect(page.locator('#net-usb-status')).toHaveText('No phone');
  await expect(page.locator('#net-firewall-status')).toHaveText('ufw firewall on');
  await expect(page.locator('#net-firewall-details code')).toHaveText('sudo ufw allow in on wlan0 to any port 5000,1935,8888,8889 proto tcp');
});

test('team names keep their own wording on the overlay and can be renamed in match mode', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.locator('#home-name').fill('Fortuna Düsseldorf U19');
  await expect(page.locator('#scoreboard-preview')).toContainText('Fortuna Düsseldorf U19');
  await page.locator('#home-name').press('Enter');
  await expect.poll(async () => (await page.request.get('/api/scoreboard')).json().then(x=>x.scoreboard.home_name)).toBe('Fortuna Düsseldorf U19');
  await page.locator('#mode-toggle').click();
  page.once('dialog', dialog => dialog.accept('SV Gäste'));
  await page.locator('#match-panel [data-bind="away_name"]').click();
  await expect(page.locator('#match-panel [data-bind="away_name"]')).toHaveText('SV Gäste');
  await expect.poll(async () => (await page.request.get('/api/scoreboard')).json().then(x=>x.scoreboard.away_name)).toBe('SV Gäste');
  await page.locator('#mode-toggle').click();
});

test('hotspot form keeps the saved password and buttons follow the hotspot state', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#net-hotspot-status')).toHaveText('Active');
  await expect(page.locator('#hotspot-password')).toHaveValue('saved-secret');
  await expect(page.locator('#hotspot-ssid')).toHaveValue('MatchdayLive');
  // Running with unchanged settings: nothing to start, only Stop.
  await expect(page.locator('#hotspot-start-btn')).toBeDisabled();
  await expect(page.locator('#hotspot-start-btn')).toHaveText('↻ Apply changes');
  await expect(page.locator('#hotspot-stop-btn')).toBeEnabled();
  await page.locator('#hotspot-password').fill('new-password-1');
  await expect(page.locator('#hotspot-start-btn')).toBeEnabled();
  await page.waitForTimeout(2500);  // a status poll must not wipe the edit
  await expect(page.locator('#hotspot-password')).toHaveValue('new-password-1');
  let asked = '';
  page.once('dialog', dialog => { asked = dialog.message(); dialog.dismiss(); });
  await page.locator('#hotspot-stop-btn').click();
  expect(asked).toContain('Stop the hotspot?');
});

test('audio meters show every input and the YouTube output', async ({page}) => {
  await page.goto('/');
  const setup = page.locator('#meters-setup');
  await expect(setup.locator('.meter-row')).toHaveCount(4);
  await expect(setup.locator('[data-meter-key="cam1"] .meter-db')).toHaveText('-12 dB');
  await expect(setup.locator('[data-meter-key="cam2"] .meter-db')).toHaveText('no audio');
  await expect(setup.locator('[data-meter-key="usb:alsa_input.usb-test"] .meter-label')).toHaveText('🎤 Test USB Mic');
  await expect(setup.locator('.meter-row').last()).toHaveAttribute('data-meter-key', 'program');
  await expect(setup.locator('[data-meter-key="program"] .meter-label')).toHaveText('Program audio');
  // -0.5 dBFS is close to clipping: highlighted red.
  await expect(setup.locator('[data-meter-key="program"] .meter-db')).toHaveCSS('color', 'rgb(248, 113, 113)');
  const cover = await setup.locator('[data-meter-key="cam1"] .meter-cover').evaluate(el => el.style.width);
  expect(cover).toBe('20%');  // -12 dB on a -60..0 scale fills 80%
  await page.locator('#mode-toggle').click();
  await expect(page.locator('#meters-match .meter-row')).toHaveCount(2);
  await expect(page.locator('#meters-match .meter-row').last()).toHaveAttribute('data-meter-key', 'program');
  await page.locator('#mode-toggle').click();
});

test('match clock can be set and nudged while it runs', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  const clock = page.locator('#match-clock-display');
  await page.getByLabel('Set clock to (mm:ss)').fill('9:00');
  await page.getByLabel('Set clock to (mm:ss)').press('Enter');
  await expect(clock).toHaveText(/^09:0\d$/);
  await page.getByRole('button', {name: '+1 min'}).first().click();
  await expect(clock).toHaveText(/^10:0\d$/);
  await page.getByRole('button', {name: '−10 s'}).first().click();
  await expect(clock).toHaveText(/^09:[56]\d$/);
  await page.getByLabel('Set clock to (mm:ss)').fill('nonsense');
  await page.getByLabel('Set clock to (mm:ss)').press('Enter');
  await expect(page.locator('#toast-container')).toContainText('mm:ss');
  await page.locator('#mode-toggle').click();
  await page.getByRole('button', {name:'Adjust game clock', exact:true}).click();
  await page.getByLabel('Minutes', {exact:true}).fill('12');
  await page.getByLabel('Seconds', {exact:true}).fill('0');
  await page.getByRole('button', {name:'Apply time', exact:true}).click();
  await expect(page.locator('#match-panel [data-bind="clock"]')).toHaveText(/^12:0\d$/);
  await page.locator('#mode-toggle').click();
});


test('compact meters follow actual mixed audio and clear muted inputs', async ({page}) => {
  let active = ['usb:alsa_input.usb-test'];
  await page.route('**/api/audio/levels', async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.audio_on_air = active;
    await route.fulfill({response, json: body});
  });
  await page.goto('/');
  await page.locator('#mode-toggle').click();
  const meters = page.locator('#meters-match');
  await expect(meters.locator('.on-air')).toHaveCount(1);
  await expect(meters.locator('.on-air')).toHaveAttribute('data-meter-key', 'usb:alsa_input.usb-test');
  active = ['cam1', 'usb:alsa_input.usb-test'];
  await expect(meters.locator('.on-air')).toHaveCount(2);
  active = [];
  await expect(meters.locator('.on-air')).toHaveCount(0);
  await expect(meters.locator('.meter-row')).toHaveCount(1);
  await expect(meters.locator('.meter-row')).toHaveAttribute('data-meter-key','program');
});

test('clipboard denial offers manual copy without a success toast', async ({page}) => {
  await page.goto('/');
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {value:{writeText:async()=>{throw Error('denied');}}, configurable:true});
    document.execCommand = () => false;
  });
  const dialogSeen = page.waitForEvent('dialog');
  const copy = page.evaluate(() => copyRtmp('rtmp://test/cam1'));
  const dialog = await dialogSeen;
  expect(dialog.message()).toContain('Automatic copy failed');
  expect(dialog.defaultValue()).toBe('rtmp://test/cam1');
  await dialog.dismiss();
  await copy;
  await expect(page.locator('#toast-container')).not.toContainText('Copied');
});

test('start is guarded against duplicate taps and STOP stays available', async ({page}) => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let starts = 0;
  await page.route('**/api/stream/start', async route => {
    starts++;
    await held;
    await route.fulfill({status:409,json:{ok:false,message:'Superseded by a newer request'}});
  });
  await page.route('**/api/stream/stop', route => route.fulfill({json:{ok:true,message:'Stopped'}}));
  await page.goto('/');
  await page.locator('#stream-key').fill('test-key');
  await page.locator('[onclick="goLive()"]').click();
  await expect(page.locator('[onclick="goLive()"]')).toBeDisabled();
  await page.evaluate(() => { goLive(); });
  await expect(page.locator('[onclick="stopStream()"]').last()).toBeEnabled();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('[onclick="stopStream()"]').last().click();
  release();
  await expect(page.locator('[onclick="goLive()"]')).toBeEnabled();
  expect(starts).toBe(1);
});


test('clock editor preserves running state, edits survive polls, and cancel does not apply', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.evaluate(() => clockAction('pause'));
  await expect.poll(async()=> (await (await page.request.get('/api/scoreboard')).json()).scoreboard.clock_running).toBe(false);
  await page.getByRole('button', {name:'✎ Adjust clock',exact:true}).last().click();
  await page.getByLabel('Minutes',{exact:true}).fill('63');
  await page.getByLabel('Seconds',{exact:true}).fill('27');
  await page.waitForTimeout(2200);
  await expect(page.getByLabel('Minutes',{exact:true})).toHaveValue('63');
  await page.screenshot({path:'/tmp/mcc-clock-editor.png'});
  await page.getByRole('button',{name:'Apply time',exact:true}).click();
  await expect(page.locator('#match-clock-display')).toHaveText('63:27');
  expect((await (await page.request.get('/api/scoreboard')).json()).scoreboard.clock_running).toBe(false);
  await page.evaluate(() => clockAction('start'));
  await expect.poll(async()=> (await (await page.request.get('/api/scoreboard')).json()).scoreboard.clock_running).toBe(true);
  await page.getByRole('button', {name:'✎ Adjust clock',exact:true}).last().click();
  await page.getByRole('button',{name:'45:00',exact:true}).click();
  await page.getByRole('button',{name:'Apply time',exact:true}).click();
  await expect(page.locator('#match-clock-display')).toHaveText(/^45:0[0-9]$/);
  expect((await (await page.request.get('/api/scoreboard')).json()).scoreboard.clock_running).toBe(true);
  await page.getByRole('button', {name:'✎ Adjust clock',exact:true}).last().click();
  await page.getByRole('button',{name:'00:00',exact:true}).click();
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(page.locator('#match-clock-display')).toHaveText(/^45:0[0-9]$/);
});


test('scoreboard switch hides only the overlay and stays in sync across modes', async ({page}) => {
  await page.goto('/');
  const setup=page.getByRole('switch',{name:'Scoreboard on video'}).last();
  await expect(setup).toBeChecked();
  await setup.uncheck();
  await expect(setup).not.toBeChecked();
  await expect(page.locator('#scoreboard-preview')).toContainText('Scoreboard hidden on video');
  await page.locator('#mode-toggle').click();
  const match=page.locator('#match-panel').getByRole('switch',{name:'Scoreboard on video'});
  await expect(match).not.toBeChecked();
  await page.locator('#match-panel').getByLabel('home goal').click();
  await expect(match).not.toBeChecked();
  await page.reload();
  await expect(match).not.toBeChecked();
  await match.check();
  await expect(match).toBeChecked();
  await page.locator('#mode-toggle').click();
  await expect(setup).toBeChecked();
  await expect(page.locator('#scoreboard-preview')).not.toContainText('hidden on video');
});


test('offline cameras show a neutral placeholder and load previews only when online', async ({page}) => {
  let online=false;
  await page.route('**/api/status',async route=>{
    const response=await route.fetch();
    const body=await response.json();
    body.cameras={cam1:online,cam2:false,cam3:false,program:true};
    await route.fulfill({response,json:body});
  });
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  for (const cam of ['cam1','cam2','cam3']) {
    await expect(page.locator(`[data-offline="${cam}"]`)).toBeVisible();
    await expect(page.locator(`[data-offline="${cam}"]`)).toHaveText('Camera offline');
    await expect(page.locator(`iframe[data-camera="${cam}"]`)).not.toHaveAttribute('src', /.+/);
  }
  await page.locator('#frame-cam1').screenshot({path:'/tmp/mcc-offline-camera.png'});
  online=true;
  await expect(page.locator('iframe[data-camera="cam1"]')).toHaveAttribute('src', /cam1$/);
  await expect(page.locator('[data-offline="cam1"]')).toBeHidden();
  online=false;
  await expect(page.locator('[data-offline="cam1"]')).toBeVisible();
  await expect(page.locator('iframe[data-camera="cam1"]')).not.toHaveAttribute('src', /.+/);
});


test('operator can apply PiP and side-by-side layouts with separate cameras', async ({page}) => {
  const sent=[];
  await page.route('**/api/layout',async route=>{
    sent.push(route.request().postDataJSON());
    await route.fulfill({json:{ok:true,message:'Layout applied'}});
  });
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await page.getByRole('button',{name:'Video layout',exact:true}).last().click();
  await page.locator('#layout-mode').selectOption('pip');
  await page.getByLabel('Main camera',{exact:true}).selectOption('cam1');
  await page.getByLabel('Second camera',{exact:true}).selectOption('cam2');
  await expect(page.getByLabel('Inset corner',{exact:true})).toBeVisible();
  await page.screenshot({path:'/tmp/mcc-layout-editor.png'});
  await page.getByRole('button',{name:'Apply layout',exact:true}).click();
  await expect(page.locator('#layout-editor')).not.toBeVisible();
  expect(sent[0]).toEqual({mode:'pip',main:'cam1',secondary:'cam2',position:'top-right'});
  await page.locator('#mode-toggle').click();
  await page.locator('#match-panel').getByRole('button',{name:'Video layout',exact:true}).click();
  await page.locator('#layout-mode').selectOption('split');
  await page.getByLabel('Main camera',{exact:true}).selectOption('cam2');
  await page.getByLabel('Second camera',{exact:true}).selectOption('cam3');
  await expect(page.getByLabel('Inset corner',{exact:true})).toBeHidden();
  await page.getByRole('button',{name:'Apply layout',exact:true}).click();
  await expect(page.locator('#layout-editor')).not.toBeVisible();
  expect(sent[1]).toEqual({mode:'split',main:'cam2',secondary:'cam3',position:'top-right'});
});
