const {test, expect} = require('@playwright/test');

test('dashboard uses real API contracts and preserves edits', async ({page}) => {
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Connected');
  await expect(page.locator('#net-hotspot-status')).toHaveText('Active');
  await expect(page.locator('#url-cam1')).toHaveText('rtmp://10.55.0.1:1935/cam1');
  await expect(page.locator('#audio-follow')).toHaveClass(/bg-brand-blue/);
  await page.locator('#home-name').fill('Test FC');
  await page.waitForTimeout(2200);
  await expect(page.locator('#home-name')).toHaveValue('Test FC');
  await page.locator('#away-name').click();
  await expect.poll(async () => (await page.request.get('/api/scoreboard')).json().then(x=>x.scoreboard.home_name)).toBe('TEST FC');
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
