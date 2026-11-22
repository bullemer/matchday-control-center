const {test, expect} = require('@playwright/test');

test('dashboard uses real API contracts and preserves edits', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await expect(page.locator('#net-hotspot-status')).toHaveText('Active');
  await expect(page.locator('#net-usb-status')).toHaveText('Phone connected');
  await expect(page.locator('#url-cam1')).toHaveText('rtmp://10.55.0.1:1935/cam1?user=camera&pass=browser-camera');
  await expect(page.locator('#audio-follow')).toHaveClass(/bg-brand-blue/);
  await page.locator('#home-name').fill('Test FC');
  await page.waitForTimeout(2200);
  await expect(page.locator('#home-name')).toHaveValue('Test FC');
  await page.locator('#away-name').click();
  await expect.poll(async () => (await page.request.get('/api/scoreboard')).json().then(x=>x.scoreboard.home_name)).toBe('Test FC');
  await page.evaluate(() => adjustScore('home',1));
  await expect(page.locator('#home-score-display')).toHaveText('1');
  let halfQuestion='';
  page.once('dialog', dialog => { halfQuestion=dialog.message(); dialog.accept(); });
  await page.evaluate(() => clockAction('second_half'));
  await expect(page.locator('#match-clock-display')).toHaveText('45:00');
  expect(halfQuestion).toContain('45:00');
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
  await expect(page.locator('iframe[data-camera="cam1"]')).toHaveAttribute('src', /:8889\/cam1$/);
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
  await page.getByRole('tab', {name: /Cams/}).click();
  await expect(page.locator('#url-cam1')).toHaveText('rtmp://192.168.1.50:1935/cam1?user=camera&pass=browser-camera');
  await page.getByRole('tab', {name: /Network/}).click();
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
  await page.getByRole('tab', {name: /Network/}).click();
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

test('Score67 source keeps edits, preserves private keys and uses the master switch', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  try {
    await page.locator('#scoreboard-source').selectOption('score67');
    await page.locator('#scoreboard-url').fill('https://score67.de/s/test?k=private&pos=tr&preview=1');
    await page.waitForTimeout(2200);
    await expect(page.locator('#scoreboard-source')).toHaveValue('score67');
    await page.locator('#scoreboard-source-save').click();
    await expect(page.locator('#scoreboard-url')).toHaveValue('https://score67.de/s/test/overlay?k=private&pos=tr');
    await expect(page.locator('#home-name')).toBeDisabled();
    await expect(page.locator('[data-scoreboard-source-status]').last()).toContainText('controlled in Score67');
    const toggle = page.locator('[data-scoreboard-visible]').last();
    await toggle.uncheck();
    await expect(page.locator('#scoreboard-preview')).toContainText('hidden on video');
    await toggle.check();
    await expect(page.locator('#scoreboard-preview')).toContainText('Score67 OBS overlay');
    await page.setViewportSize({width:390,height:844});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({path:'/tmp/mcc-score67-settings.png',fullPage:true});
  } finally {
    await page.request.post('/api/scoreboard/update',{data:{source:'builtin',visible:true,overlay_url:''}});
  }
});

test('outdoor sunlight high contrast mode toggles and persists across page reloads', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await expect(page.locator('body')).not.toHaveClass(/outdoor-mode/);
  await expect(page.locator('#outdoor-toggle')).toHaveText('☀️ Outdoor');

  await page.locator('#outdoor-toggle').click();
  await expect(page.locator('body')).toHaveClass(/outdoor-mode/);
  await expect(page.locator('#outdoor-toggle')).toHaveText('🌙 Dark mode');

  await page.reload();
  await expect(page.locator('body')).toHaveClass(/outdoor-mode/);
  await expect(page.locator('#outdoor-toggle')).toHaveText('🌙 Dark mode');

  await page.locator('#outdoor-toggle').click();
  await expect(page.locator('body')).not.toHaveClass(/outdoor-mode/);
  await expect(page.locator('#outdoor-toggle')).toHaveText('☀️ Outdoor');
});

test('camera cards show Tally and QR modal with vector SVG and field router instructions', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.getByRole('tab', {name: /Cams/}).click();

  const cam1QRBtn = page.getByRole('button', {name: '📱 Tally & QR'}).first();
  await cam1QRBtn.click();

  const qrDialog = page.locator('#qr-dialog');
  await expect(qrDialog).toBeVisible();
  await expect(page.locator('#qr-modal-title')).toContainText('Cam 1');
  await expect(page.locator('#qr-svg-container svg')).toBeVisible();
  await expect(page.locator('#qr-tally-url')).toContainText('/tally/cam1');
  await expect(page.locator('#qr-rtmp-url')).toContainText('/cam1');
  await expect(page.locator('#qr-network-guide')).toBeVisible();

  await page.locator('#qr-tab-cam2').click();
  await expect(page.locator('#qr-modal-title')).toContainText('Cam 2');
  await expect(page.locator('#qr-tally-url')).toContainText('/tally/cam2');

  await qrDialog.getByRole('button', {name: 'Done'}).click();
  await expect(qrDialog).toBeHidden();
});

test('operator mobile tally screen loads without basic auth and displays camera state', async ({browser}) => {
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto('/tally/cam1');
  await expect(page.locator('#cam-title')).toHaveText('CAM 1');
  await expect(page.locator('body')).toContainText('from the control dashboard');
  await expect(page.locator('#tally-box')).toBeVisible();
  await context.close();
});

test('tally tells the camera a replay returns to that it is about to be live again', async ({browser}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const tally = {ok: true, cam_id: 'cam1', publishing: true, on_air: false, selected: false, secondary: false,
                 stream_active: true, replay: true, returning: true, replay_hold: false};
  await page.route('**/api/tally/cam1', route => route.fulfill({json: tally}));
  await page.goto('/tally/cam1');
  await expect(page.locator('#tally-status')).toContainText('REPLAY ON AIR');
  await expect(page.locator('#tally-hint')).toContainText('live again in a moment');
  await expect(page.locator('#tally-box')).toHaveClass(/tally-replay/);
  Object.assign(tally, {replay: false, returning: false, on_air: true});
  await expect(page.locator('#tally-status')).toContainText('LIVE ON AIR');
  await context.close();
});


test('setup tabs group sections, persist the choice and play previews only on Main', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('tab', {name: /Main/})).toHaveAttribute('aria-selected', 'true');
  for (const title of ['Multiview', 'Scoreboard & Match Clock', 'Live Stream & Recording']) {
    await expect(page.getByRole('heading', {name: title})).toBeVisible();
  }
  for (const title of ['Network Controls', 'Camera RTMP Links', 'Sideline Checklist', 'Recordings & Highlights']) {
    await expect(page.getByRole('heading', {name: title})).toBeHidden();
  }
  await page.getByRole('tab', {name: /Cams/}).click();
  await expect(page.getByRole('heading', {name: 'Camera RTMP Links'})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Multiview'})).toBeHidden();
  await expect(page.locator('iframe[data-preview="setup"][src]')).toHaveCount(0);
  await page.getByRole('tab', {name: /Checklist/}).click();
  await page.reload();
  await expect(page.getByRole('tab', {name: /Checklist/})).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('heading', {name: 'Sideline Checklist'})).toBeVisible();
  await page.getByRole('tab', {name: /Main/}).click();
});

test('master mute silences the broadcast from Main and match mode and stays in sync', async ({page}) => {
  await page.goto('/');
  const setupMute = page.locator('section[data-tab="main"] [data-mute]');
  const matchMute = page.locator('#match-panel [data-mute]');
  await expect(setupMute).toHaveAttribute('aria-pressed', 'false');
  await setupMute.click();
  await expect(setupMute).toHaveText(/MUTED/);
  await expect(setupMute).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(async () => (await page.request.get('/api/audio/levels')).json().then(x => x.mixer.muted)).toBe(true);
  await page.waitForTimeout(2500);  // status polls keep the muted state
  await expect(setupMute).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#mode-toggle').click();
  await expect(matchMute).toHaveAttribute('aria-pressed', 'true');
  await expect(matchMute).toHaveText('🔇 MUTED');
  await page.keyboard.press('m');
  await expect(matchMute).toHaveAttribute('aria-pressed', 'false');
  await expect(matchMute).toHaveText('🔊 Mute');
  await expect.poll(async () => (await page.request.get('/api/audio/levels')).json().then(x => x.mixer.muted)).toBe(false);
  await page.locator('#mode-toggle').click();
});

test('recording controls, highlight marking and the Recordings tab', async ({page}) => {
  await page.goto('/');
  await page.getByRole('tab', {name: /Main/}).click();
  const start = page.locator('[data-rec-start]');
  await expect(start).toHaveText('⏺ Record only (no YouTube)');
  await expect(page.locator('#record-with-live')).toBeChecked();
  await start.click();
  await expect(page.locator('#status-rec-badge')).toBeVisible();
  await expect(page.locator('section[data-tab="main"] [data-bind="rec_status"]')).toHaveText('00:01:05 · 150 MB · 1 highlight');
  await page.locator('section[data-tab="main"] .mark-btn').click();
  await expect(page.locator('#toast-container')).toContainText('Highlight marked at');
  // Match mode: REC badge, ⭐ and the H key.
  await page.locator('#mode-toggle').click();
  await expect(page.locator('#match-panel [data-rec-badge]')).toBeVisible();
  await page.keyboard.press('h');
  await expect(page.locator('#match-panel [aria-label="Mark highlight"]')).toBeVisible();
  await page.setViewportSize({width: 390, height: 844});  // REC row must not widen the phone layout
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.setViewportSize({width: 1280, height: 720});
  await page.locator('#mode-toggle').click();

  await page.getByRole('tab', {name: /Recordings/}).click();
  await expect(page.locator('[data-bind="rec_free"]')).toContainText('40.0 GB free');
  const cards = page.locator('#recordings-list [data-rec]');
  await expect(cards).toHaveCount(2);
  const live = cards.nth(0), done = cards.nth(1);
  await expect(live).toContainText('Polonia vs Altona');
  await expect(live.locator('.rec-badge')).toHaveText('⏺ REC');
  await expect(live.locator('[data-marker]')).toHaveCount(3);  // fixture's Chance, ⭐ button and H key
  await expect(live.getByRole('button', {name: /Delete/})).toHaveCount(0);  // cannot delete while recording
  await expect(done.locator('.chip')).toHaveText('Ready');
  await expect(done.getByRole('link', {name: /Download MP4/})).toHaveAttribute('href', /20261101_Polonia-vs-HEBC\.mp4\?download=1$/);
  await expect(done.locator('[data-marker]').first()).toContainText("12:30' · Polonia 1-0 HEBC");
  await done.locator('summary').click();
  await expect(done.locator('textarea')).toHaveValue('0:00 Kick-off\n12:30 Goal – Polonia 1-0 HEBC (12:30)\n40:00 Highlight – Polonia 1-0 HEBC (40:02)');
  // Relabel persists; the list refreshes without losing it.
  await done.locator('[data-marker]').nth(1).locator('select').selectOption('Save');
  await expect.poll(async () => (await page.request.get('/api/recordings')).json()
    .then(x => x.recordings[1].markers[1].label)).toBe('Save');
  await page.waitForTimeout(3500);
  await expect(done.locator('[data-marker]').nth(1).locator('select')).toHaveValue('Save');
  page.once('dialog', dialog => dialog.accept());
  await done.locator('[data-marker]').nth(1).getByRole('button', {name: 'Remove highlight'}).click();
  await expect(done.locator('[data-marker]')).toHaveCount(1);

  await page.getByRole('tab', {name: /Main/}).click();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('section[data-tab="main"]').getByRole('button', {name: '⏹ Stop recording'}).click();
  await expect(page.locator('#status-rec-badge')).toBeHidden();
  await expect(start).toBeVisible();
});

test('instant replay controls follow the recording and drive the replay API', async ({page}) => {
  await page.goto('/');
  await page.getByRole('tab', {name: /Main/}).click();
  const multiview = page.locator('section[data-tab="main"]').first();
  await expect(multiview.getByRole('button', {name: '⏪ Replay'})).toBeHidden();
  await expect(multiview.getByText('Replays need the recording on')).toBeVisible();
  await page.locator('[data-rec-start]').click();
  await expect(multiview.getByRole('button', {name: '⏪ Replay'})).toBeVisible();
  await page.locator('#replay-seconds').selectOption('15');
  await multiview.getByRole('button', {name: '🐢 Slow-mo'}).click();
  await expect(page.locator('#status-replay-badge')).toBeVisible();
  await expect(multiview.locator('[data-bind="replay_status"]')).toContainText('Slow-mo replay on air');
  await expect(page.locator('[data-cut="cam1"] [data-cut-state]').first()).toHaveText('Back after replay');
  await multiview.getByRole('button', {name: '⏹ Back to live'}).click();
  await expect(page.locator('#status-replay-badge')).toBeHidden();
  // Match mode buttons and keys; the highlight replay in the Recordings tab.
  await page.locator('#mode-toggle').click();
  await expect(page.locator('#match-panel [aria-label="Instant replay"]')).toBeVisible();
  await page.keyboard.press('r');
  await expect(page.locator('#match-panel [data-replay-badge]')).toBeVisible();
  await page.locator('#match-panel').getByRole('button', {name: '⏹ Live'}).click();
  await page.keyboard.press('s');
  await page.locator('#match-panel').getByRole('button', {name: '⏹ Live'}).click();
  await page.locator('#mode-toggle').click();
  await page.getByRole('tab', {name: /Recordings/}).click();
  const live = page.locator('#recordings-list [data-rec]').first();
  await live.locator('[data-marker]').first().getByRole('button', {name: '⏪ Replay'}).click();
  await expect(page.locator('#status-replay-badge')).toBeVisible();
  const done = page.locator('#recordings-list [data-rec]').nth(1);
  await expect(done.getByRole('button', {name: '⏪ Replay'})).toHaveCount(0);  // only the running match
  const calls = await (await page.request.get('/test/replay-calls')).json();
  expect(calls.slice(0, 3)).toEqual([[15, 0.5, null], [15, 1, null], [15, 0.5, null]]);
  expect(calls[3][2]).toMatch(/^m1-/);
  await page.request.post('/api/replay/stop', {data: {}});
  await page.getByRole('tab', {name: /Main/}).click();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('section[data-tab="main"]').getByRole('button', {name: '⏹ Stop recording'}).click();
});

test('stream to several platforms at once, each with its own key, status and stop', async ({page}) => {
  await page.goto('/');
  await page.getByRole('tab', {name: /Main/}).click();
  const row = name => page.locator(`[data-dest="${name}"]`);
  const need = page.locator('[data-bind="upload_need"]');
  await expect(row('youtube').locator('[data-dest-enabled]')).toBeChecked();  // YouTube on by default
  await expect(row('facebook').locator('[data-dest-key]')).toBeHidden();
  await expect(need).toHaveText('Upload needed: about 4.1 Mbit/s (1 × 4.1).');
  await expect(page.locator('[data-dest-start]:visible, [data-dest-stop]:visible')).toHaveCount(0);  // not streaming yet
  for (const name of ['facebook', 'twitch']) await row(name).locator('[data-dest-enabled]').check();
  await expect(need).toContainText('about 12.4 Mbit/s (3 × 4.1). That is a lot for mobile data — consider a lower quality');
  await expect(row('twitch').locator('[data-dest-server]')).toHaveValue('rtmps://ingest.global-contribute.live-video.net/app');
  await page.getByRole('button', {name: '🔴 GO LIVE'}).click();
  await expect(page.locator('#toast-container')).toContainText('Enter the stream key for ▶️ YouTube, 📘 Facebook, 🟣 Twitch');
  await row('youtube').locator('[data-dest-key]').fill('yt-key');
  await row('facebook').locator('[data-dest-key]').fill('FB-1?s_bl=1&a=Xy');
  await row('twitch').locator('[data-dest-key]').fill('live_42_abc');
  await row('twitch').locator('summary').click();
  await row('twitch').locator('[data-dest-server]').fill('rtmps://euc10.contribute.live-video.net/app');
  await page.getByRole('button', {name: '🔴 GO LIVE'}).click();
  await expect(row('youtube').locator('[data-dest-state]')).toHaveText('LIVE');
  await expect(row('facebook').locator('[data-dest-state]')).toHaveText('RECONNECTING…');
  await expect(row('facebook').locator('[data-dest-error]')).toHaveText('⚠ Connection refused');
  let calls = await (await page.request.get('/test/stream-calls')).json();
  expect(calls.calls[0]).toEqual({profile: '720p', record: true, urls: {
    youtube: 'rtmps://a.rtmps.youtube.com:443/live2/yt-key',
    facebook: 'rtmps://live-api-s.facebook.com:443/rtmp/FB-1?s_bl=1&a=Xy',
    twitch: 'rtmps://euc10.contribute.live-video.net/app/live_42_abc'}});
  // Match mode shows a status chip per platform.
  await page.locator('#mode-toggle').click();
  await expect(page.locator('#match-panel [data-dest-summary] span')).toHaveText(['YT', 'FB', 'TW']);
  await page.locator('#mode-toggle').click();
  // Add Custom RTMP while live: only that one is started.
  await row('custom').locator('[data-dest-enabled]').check();
  await row('custom').locator('[data-dest-server]').fill('rtmp://10.0.0.2/live');
  await row('custom').locator('[data-dest-key]').fill('club');
  await row('custom').getByRole('button', {name: '▶ Start'}).click();
  await expect(row('custom').locator('[data-dest-state]')).toHaveText('LIVE');
  await expect(row('custom').getByRole('button', {name: '▶ Start'})).toBeHidden();
  await expect(row('youtube').getByRole('button', {name: '▶ Start'})).toBeHidden();  // already live
  calls = await (await page.request.get('/test/stream-calls')).json();
  expect(calls.calls[1].urls).toEqual({custom: 'rtmp://10.0.0.2/live/club'});
  // Stop one platform; the others stay.
  page.once('dialog', dialog => dialog.accept());
  await row('facebook').getByRole('button', {name: '⏹ Stop'}).click();
  await expect(row('facebook').locator('[data-dest-state]')).toBeHidden();
  await expect(row('youtube').locator('[data-dest-state]')).toHaveText('LIVE');
  // Choices and servers survive a reload; the keys used to go live are saved on the laptop.
  await page.reload();
  await expect(row('twitch').locator('[data-dest-enabled]')).toBeChecked();
  await expect(row('custom').locator('[data-dest-server]')).toHaveValue('rtmp://10.0.0.2/live');
  await expect(row('youtube').locator('[data-dest-key]')).toHaveValue('yt-key');
  await expect(row('twitch').locator('[data-dest-key]')).toHaveValue('live_42_abc');
  // Switching "remember" off forgets them on the laptop.
  await page.locator('#remember-keys').uncheck();
  await expect(page.locator('#toast-container')).toContainText('Saved stream keys removed');
  await page.reload();
  await expect(page.locator('#remember-keys')).not.toBeChecked();
  await expect(row('youtube').locator('[data-dest-key]')).toHaveValue('');
  await page.locator('#remember-keys').check();
  calls = await (await page.request.get('/test/stream-calls')).json();
  expect(calls.stops).toEqual(['facebook']);
  await page.request.post('/api/stream/stop', {data: {}});
  for (const name of ['facebook', 'twitch', 'custom']) await row(name).locator('[data-dest-enabled]').uncheck();
});

test('CAM REPLAY: scrub the recording, play from a moment and stay on air until a cut', async ({page}) => {
  await page.goto('/');
  await page.getByRole('tab', {name: /Main/}).click();
  const panel = page.locator('#replay-panel');
  const cut = page.locator('#btn-cut-replay');
  await expect(cut).toBeDisabled();
  await expect(cut.locator('[data-cut-state]')).toHaveText('Needs recording');
  await page.locator('[data-rec-start]').click();
  await expect(cut).toBeEnabled();
  const slider = page.locator('#replay-cue');
  await expect.poll(async () => Number(await slider.getAttribute('max'))).toBeGreaterThan(115);  // ~118.5 s replayable
  await expect(panel.locator('[data-bind="replay_cue_behind"]')).toHaveText('(10 s before live)');
  await panel.getByRole('button', {name: '◀ 10 s'}).click();
  await expect(panel.locator('[data-bind="replay_cue_behind"]')).toHaveText(/\((19|20|21) s before live\)/);
  await panel.locator('.replay-tick[title^="Chance"]').click();  // the fixture's highlight, ~60 s in
  const cue = Number(await slider.inputValue());
  expect(cue).toBeGreaterThan(53); expect(cue).toBeLessThan(57);  // 5 s before the mark
  await expect(page.locator('#replay-still-msg')).toHaveText('No picture at that moment yet');  // fixture has no video
  await panel.getByRole('button', {name: '🐢 Play slow'}).click();
  await expect(page.locator('#status-replay-badge')).toBeVisible();
  await expect(cut.locator('[data-cut-state]')).toHaveText('On air · slow');
  await expect(page.locator('[data-cut="cam1"] [data-cut-state]').first()).toHaveText('Ready');  // no automatic return
  await expect(panel.locator('[data-bind="replay_status"]')).toContainText('cut to a camera to end it');
  let calls = await (await page.request.get('/test/replay-calls')).json();
  expect(calls[calls.length - 1].slice(1, 3)).toEqual([0.5, null]);
  expect(calls[calls.length - 1][3]).toBeCloseTo(cue, 0);
  expect(calls[calls.length - 1][4]).toBe(true);
  await panel.getByRole('button', {name: '⏹ Back to live'}).click();
  await expect(cut.locator('[data-cut-state]')).toHaveText(/^Ready · 0:5\d$/);  // keeps the chosen moment
  // Match mode: REPLAY cut button and key 4 use the same moment and the last speed.
  await page.locator('#mode-toggle').click();
  await page.keyboard.press('0');
  await expect(page.locator('#match-panel [data-cut-replay] [data-cut-state]')).toHaveText('On air · slow');
  calls = await (await page.request.get('/test/replay-calls')).json();
  expect(calls[calls.length - 1][3]).toBeCloseTo(cue, 0);
  await page.locator('#match-panel').getByRole('button', {name: '⏹ Live'}).click();
  await page.locator('#mode-toggle').click();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('section[data-tab="main"]').getByRole('button', {name: '⏹ Stop recording'}).click();
});

test('encoder choice asks the server and goes back when it is refused', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  const select = page.locator('#hw-encode');
  await expect(select).toHaveValue('software');
  let asked = null;
  await page.route('**/api/encoder', route => {
    asked = route.request().postDataJSON();
    route.fulfill({status: 409, json: {ok: false, message: 'No working NVIDIA encoder found; staying on the current encoder'}});
  });
  await select.selectOption('nvidia');
  await expect(page.locator('body')).toContainText('No working NVIDIA encoder');
  expect(asked).toEqual({encoder: 'nvidia'});
  await expect(select).toHaveValue('software');
});

test('match camera mount, wide shot, separate recording switch', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.locator('[data-tab-btn="cams"]').click();
  await expect(page.locator('[data-cam-edit="mount"]')).toHaveCount(4);  // match cameras only
  const mount = page.waitForRequest(r => r.url().endsWith('/api/cameras/cam3') && r.postDataJSON().mount === 'tracking');
  await page.locator('[data-cam-edit="mount"][data-cam="cam3"]').selectOption('tracking');
  await mount;
  await page.locator('[data-cam-edit="wide"][data-cam="cam1"]').check();
  await page.locator('[data-cam-edit="wide"][data-cam="cam2"]').check();
  await expect(page.locator('[data-cam-edit="wide"][data-cam="cam1"]')).not.toBeChecked();  // one wide shot
  await page.locator('[data-tab-btn="main"]').click();
  let iso = null;
  await page.route('**/api/recording/match-cameras', route => {
    iso = route.request().postDataJSON();
    route.fulfill({json: {ok: true, message: 'Match cameras are recorded separately too', recording: {active: false, iso_match: true}}});
  });
  await page.locator('#iso-match').check();
  await expect(page.locator('body')).toContainText('recorded separately too');
  expect(iso).toEqual({on: true});
  expect(errors).toEqual([]);
});

test('output preview, added time and microphone delay', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  // Not live: the output tile says when it starts.
  await expect(page.locator('[data-offline="output"]')).toHaveText('Starts with GO LIVE or recording');
  await expect(page.locator('iframe[data-output]')).toBeHidden();
  // Announced added time shows in the clock as on TV.
  await page.locator('#added-time').selectOption('3');
  await expect(page.locator('#match-clock-display')).toContainText('(+3)');
  expect((await (await page.request.get('/api/scoreboard')).json()).scoreboard.added).toBe(3);
  await page.locator('#added-time').selectOption('0');
  await expect(page.locator('#match-clock-display')).not.toContainText('(+');
  // Microphone delay goes to the server.
  await page.evaluate(() => setMicDelay('1500'));
  await expect(page.locator('#toast-container')).toContainText('Microphone delay 1.5 s');
  expect(await (await page.request.get('/test/mic-delay')).json()).toContain(1500);
  // Match mode: the preview switches between program and output while live.
  await page.evaluate(() => toggleMatchOutput());
  await expect(page.locator('#match-output-toggle')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#toast-container')).toContainText('The output starts with GO LIVE or recording');
  expect(errors).toEqual([]);
});

test('noise gate and headset monitor', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  // Listening needs a headset; the USB headset is offered first.
  await page.locator('#monitor-toggle').click();
  await expect(page.locator('#monitor-toggle')).toHaveText('Off');
  await expect(page.locator('#monitor-output option').nth(1)).toHaveText(/USB Headset/);
  await page.locator('#monitor-output').selectOption('alsa_output.pci.analog');
  await expect(page.locator('#monitor-warning')).toContainText('loudspeakers feed the sound back');
  await page.locator('#monitor-output').selectOption('alsa_output.usb-headset');
  await expect(page.locator('#monitor-warning')).toBeHidden();
  await page.locator('#monitor-toggle').click();
  await expect(page.locator('#monitor-toggle')).toHaveText('On');
  await expect(page.locator('#monitor-toggle')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('[data-monitor-mode="full"]').click();
  await expect(page.locator('[data-monitor-mode="full"]')).toHaveClass(/bg-brand-blue/);
  await expect(page.locator('#monitor-hint')).toContainText('1–3 s late');
  // Gate: threshold and switch.
  await page.locator('#gate-threshold').fill('-38');
  await expect(page.locator('#gate-threshold-value')).toHaveText('-38 dB');
  await page.locator('#gate-on').uncheck();
  await expect(page.locator('#gate-threshold')).toBeDisabled();
  const calls = await (await page.request.get('/test/audio-calls')).json();
  expect(calls).toEqual([
    ['monitor', null, null, 'alsa_output.pci.analog'], ['monitor', null, null, 'alsa_output.usb-headset'],
    ['monitor', true, null, 'alsa_output.usb-headset'], ['monitor', null, 'full', null],
    ['gate', null, -38], ['gate', false, null]]);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(errors).toEqual([]);
});

test('four match and two reporting cameras: cut row, keys, layout rule and camera cards', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.locator('#mode-toggle').click();
  const panel = page.locator('#match-panel');
  for (const label of ['CAM 1', 'CAM 2', 'CAM 3', 'CAM 4', 'REP 1', 'REP 2', 'REPLAY'])
    await expect(panel.locator('.match-cut-grid').getByText(label, {exact: true})).toBeVisible();
  // Reporting cameras and replay sit in a second row under the match cameras.
  const cam1 = await panel.locator('[data-cut="cam1"]').boundingBox();
  const rep1 = await panel.locator('[data-cut="cam5"]').boundingBox();
  expect(rep1.y).toBeGreaterThan(cam1.y + cam1.height - 1);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.locator('#match-preview-toggle').click();
  const box = await panel.boundingBox();
  console.log('six-camera panel bottom', box.y + box.height, await page.evaluate(() => scrollY));
  expect(box.y + box.height).toBeLessThanOrEqual(844);
  await page.screenshot({path:'/tmp/mcc-six-cameras.png'});
  // Keys 1-4 match cameras, 5-6 reporting cameras.
  const cut = page.waitForRequest('**/api/switch/cam5');
  await page.keyboard.press('5');
  await cut;
  // Picture-in-picture: two match cameras cannot be combined.
  await page.locator('#match-panel').getByRole('button', {name: 'Video layout'}).click();
  await page.locator('#layout-mode').selectOption('pip');
  await page.locator('#layout-main').selectOption('cam1');
  const optionOff = cam => page.locator(`#layout-secondary option[value="${cam}"]`).evaluate(o => o.disabled);
  for (const cam of ['cam2', 'cam3', 'cam4']) expect(await optionOff(cam)).toBe(true);
  expect(await optionOff('cam5')).toBe(false);
  await expect(page.locator('#layout-secondary')).toHaveValue(/cam[56]/);
  await page.locator('#layout-main').selectOption('cam5');  // interview big: any second camera
  expect(await optionOff('cam2')).toBe(false);
  await page.keyboard.press('Escape');
  // Setup: camera cards in two groups, names editable, reporting cameras' sound switch.
  await page.locator('#mode-toggle').click();
  await page.locator('[data-tab-btn="cams"]').click();
  await expect(page.getByText('🎤 Reporting cameras')).toBeVisible();
  await expect(page.locator('[data-camera-card]')).toHaveCount(6);
  await expect(page.locator('[data-report-sound]')).toHaveCount(2);
  await expect(page.locator('#url-cam6')).toContainText(':1935/cam6');
  const saved = page.waitForRequest(req => req.url().endsWith('/api/cameras/cam2') && req.postDataJSON().position === 'Left goal');
  await page.locator('[data-cam-edit="position"][data-cam="cam2"]').fill('Left goal');
  await page.locator('[data-cam-edit="position"][data-cam="cam2"]').press('Tab');
  await saved;
  expect(errors).toEqual([]);
});

test('camera input settings: SRT with delay and encryption, recommended quality, live input line', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await page.locator('[data-tab-btn="cams"]').click();
  const card = page.locator('[data-camera-card="cam5"]');
  await expect(card.locator('[data-cam-recommended]')).toHaveText('1280x720 · 30 fps · 3 Mbit/s · keyframe 1 s · H.264');
  await expect(page.locator('#url-cam5')).toHaveText(/^rtmp:\/\/.*:1935\/cam5\?user=camera&pass=browser-camera$/);
  await expect(card.locator('[data-srt-settings]')).toBeHidden();
  await card.getByRole('button', {name: 'SRT'}).click();
  await expect(card.locator('[data-srt-settings]')).toBeVisible();
  await expect(page.locator('#url-cam5')).toHaveText(/^srt:\/\/.*:8890\?streamid=publish%3Acam5%3Acamera%3Abrowser-camera&latency=300000$/);
  await card.locator('[data-cam-edit="latency_ms"]').fill('500');
  await card.locator('[data-cam-edit="latency_ms"]').press('Tab');
  await expect(page.locator('#url-cam5')).toHaveText(/latency=500000$/);
  await card.locator('[data-cam-edit="encrypt"]').check();
  await expect(page.locator('#url-cam5')).toHaveText(/&passphrase=[A-Za-z0-9_-]{16}$/);
  await card.locator('[data-cam-edit="quality"]').selectOption('720p-low');
  await expect(card.locator('[data-cam-recommended]')).toHaveText('1280x720 · 30 fps · 1.8 Mbit/s · keyframe 1 s · H.264 or H.265');
  // The QR dialog lists the separate SRT fields for apps like Larix.
  await card.getByRole('button', {name: /Tally/}).click();
  const fields = page.locator('#qr-srt-fields');
  await expect(fields).toBeVisible();
  await expect(fields).toContainText('Stream ID');
  await expect(fields).toContainText('publish:cam5:camera:browser-camera');
  await expect(fields).toContainText('500 ms');
  await expect(fields).toContainText('Passphrase');
  await expect(page.locator('#qr-recommended')).toContainText('1.8 Mbit/s');
  await page.locator('#qr-dialog').getByRole('button', {name: 'Done'}).click();
  // What actually arrives from a publishing camera.
  await expect(page.locator('[data-cam-input="cam1"]')).toHaveText('📥 SRT · H265 1920×1080 · 5.8 Mbit/s · loss 0.4 % · RTT 9 ms');
  // Back to RTMP: the SRT fields hide again; settings survive a reload.
  await page.reload();
  await page.locator('[data-tab-btn="cams"]').click();
  await expect(page.locator('#url-cam5')).toHaveText(/^srt:/);
  await card.getByRole('button', {name: 'RTMP'}).click();
  await expect(card.locator('[data-srt-settings]')).toBeHidden();
  await expect(page.locator('#url-cam5')).toHaveText(/^rtmp:/);
  expect(errors).toEqual([]);
});
