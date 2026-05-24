import { chromium } from 'playwright';

const EMAIL     = process.env.ACL_EMAIL;
const PASSWORD  = process.env.ACL_PASSWORD;
const SERVER_ID = process.env.ACL_SERVER_ID;
const TG_TOKEN  = process.env.TG_BOT_TOKEN;
const TG_CHAT   = process.env.TG_CHAT_ID;
const PROXY_SRV = 'socks5://127.0.0.1:1080';
const BASE_URL  = 'https://dash.aclclouds.com';

async function tgNotify(msg) {
  if (!TG_TOKEN || !TG_CHAT) { console.log('[TG] 未配置，跳过'); return; }
  try {
    const res = await fetch('https://api.telegram.org/bot' + TG_TOKEN + '/sendMessage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: TG_CHAT, text: msg, parse_mode: 'HTML' }),
    });
    const d = await res.json();
    console.log(d.ok ? '[TG] 已发送' : '[TG] 失败: ' + d.description);
  } catch (e) { console.error('[TG] 异常:', e.message); }
}

function parseHours(text) {
  const t = text || '';
  const days  = parseInt((t.match(/(\d+)\s*j/)   || [])[1] || '0', 10);
  const hours = parseInt((t.match(/(\d+)\s*h/)   || [])[1] || '0', 10);
  const mins  = parseInt((t.match(/(\d+)\s*min/) || [])[1] || '0', 10);
  return days * 24 + hours + mins / 60;
}

async function getRemainText(page) {
  const raw = await page.locator('text=Temps restant').first().textContent();
  console.log('[时间] 原始文本:', raw);
  const m = raw.match(/\d+\s*j\s*\d+\s*h\s*\d+\s*min/);
  return m ? m[0] : raw;
}

async function saveScreenshot(page, name) {
  try {
    await page.screenshot({ path: name, fullPage: true });
    console.log('[截图] 已保存:', name);
  } catch (e) { console.log('[截图] 失败:', e.message); }
}

// 随机整数
function randInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

(async () => {
  console.log('[代理]', PROXY_SRV);
  let browser;

  try {
    browser = await chromium.launch({
      headless: true,
      proxy: { server: PROXY_SRV },
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
      ],
    });

    const ctx = await browser.newContext({
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
      viewport: { width: 1280, height: 800 },
      locale: 'zh-CN',
    });

    const page = await ctx.newPage();
    page.setDefaultTimeout(60000);

    // ── 拦截 /auth/captcha 响应，获取 captcha_token ──
    let captchaToken  = '';
    let captchaAnswer = '';

    page.on('response', async resp => {
      const url = resp.url();
      if (url.includes('/auth/captcha')) {
        try {
          const json = await resp.json();
          console.log('[Captcha响应]', JSON.stringify(json));
          // 服务端返回 token 和 answer（如果 passed:true）
          if (json.token)  captchaToken  = json.token;
          if (json.answer) captchaAnswer = json.answer;
          if (json.passed === true) {
            console.log('[Captcha] ✅ passed! token:', captchaToken);
          }
        } catch (_) {}
      }
    });

    // ── Step 1: 打开登录页 ──
    console.log('[1] 打开登录页...');
    await page.goto(BASE_URL + '/auth/login', { waitUntil: 'networkidle', timeout: 60000 });
    await saveScreenshot(page, 'debug-login.png');

    // ── Step 2 & 3: 填邮箱、密码 ──
    console.log('[2] 填写邮箱密码...');
    await page.waitForSelector('#username', { timeout: 30000 });
    await page.locator('#username').fill(EMAIL);
    await page.locator('#password').fill(PASSWORD);

    // ── Step 4: 模拟真实用户行为后再调用 /auth/captcha ──
    // 分析 captcha 接口收集：mouse_movements, mouse_distance, clicks, key_presses, elapsed_ms
    // 需要伪造成真实用户的数值

    console.log('[4] 模拟用户行为...');

    // 在页面上做真实鼠标移动（让 Playwright 产生真实事件）
    const startTime = Date.now();

    // 模拟移动鼠标到各个位置
    await page.mouse.move(400, 300);
    await page.waitForTimeout(randInt(100, 200));
    await page.mouse.move(320, 370, { steps: 10 });
    await page.waitForTimeout(randInt(100, 200));
    await page.mouse.move(320, 595, { steps: 15 }); // 移向 checkbox
    await page.waitForTimeout(randInt(200, 400));

    // 用 JS 拦截并替换 captcha fetch，注入伪造的行为数据，直接调用 /auth/captcha
    console.log('[4] 直接 POST /auth/captcha（携带伪造行为数据）...');
    const captchaResult = await page.evaluate(async () => {
      const elapsed = Math.floor(Math.random() * 3000) + 4000; // 4~7 秒
      const payload = {
        mouse_movements: Math.floor(Math.random() * 30) + 20,   // 20~50
        mouse_distance:  Math.floor(Math.random() * 500) + 300, // 300~800
        clicks:          1,
        key_presses:     Math.floor(Math.random() * 5) + 8,     // 8~12（填邮箱密码）
        elapsed_ms:      elapsed,
      };
      console.log('captcha payload:', JSON.stringify(payload));
      const r = await fetch('/auth/captcha', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(payload),
      });
      const json = await r.json();
      console.log('captcha response:', JSON.stringify(json));
      return json;
    });

    console.log('[Captcha结果]', JSON.stringify(captchaResult));

    if (captchaResult.token)  captchaToken  = captchaResult.token;
    if (captchaResult.answer) captchaAnswer = captchaResult.answer;

    const captchaPassed = captchaResult.passed === true;
    console.log('[Captcha] passed:', captchaPassed, '| token:', captchaToken, '| answer:', captchaAnswer);

    // 同时真实点击 checkbox（双保险）
    try {
      await page.locator('.auth-captcha-inner').click({ timeout: 3000 });
      console.log('[4] 真实点击 checkbox 成功');
    } catch (_) {}

    await page.waitForTimeout(randInt(500, 1000));
    await saveScreenshot(page, 'debug-before-signin.png');

    // ── Step 5: 直接调用登录 API（带上 captcha_token 和 captcha_answer）──
    console.log('[5] 直接 POST /auth/login...');
    const loginResult = await page.evaluate(async (args) => {
      const r = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          user:           args.email,
          password:       args.password,
          captcha_token:  args.token,
          captcha_answer: args.answer,
        }),
      });
      const text = await r.text();
      console.log('login response status:', r.status, text.slice(0, 200));
      return { status: r.status, body: text };
    }, { email: EMAIL, password: PASSWORD, token: captchaToken, answer: captchaAnswer });

    console.log('[登录API] status:', loginResult.status, '| body:', loginResult.body.slice(0, 200));

    if (loginResult.status !== 200 && loginResult.status !== 201 && loginResult.status !== 302) {
      // 登录 API 失败，截图然后抛出
      await saveScreenshot(page, 'debug-login-failed.png');
      throw new Error('登录 API 失败: ' + loginResult.status + ' ' + loginResult.body.slice(0, 100));
    }

    // 登录成功，跳转到服务器页面
    // session cookie 已由浏览器上下文保存
    const serverUrl = BASE_URL + '/server/' + SERVER_ID;
    console.log('[→] 跳转到:', serverUrl);
    await page.goto(serverUrl, { waitUntil: 'networkidle', timeout: 60000 });
    await saveScreenshot(page, 'debug-server.png');
    console.log('[当前URL]', page.url());

    // 如果被重定向回登录页说明 session 未生效
    if (page.url().includes('/auth/login')) {
      throw new Error('跳转后被重定向回登录页，session 未生效');
    }

    // ── 读取剩余时间 ──
    await page.waitForSelector('text=Temps restant', { timeout: 30000 });
    const remainText  = await getRemainText(page);
    const remainHours = parseHours(remainText);
    console.log('[时间]', remainText, '→', remainHours.toFixed(1), '小时');

    // ── 续期判断 ──
    if (remainHours <= 24) {
      console.log('[续期] 剩余 ≤ 1 天，续期中...');
      const btn = page.locator('button:has-text("Renouveler"), a:has-text("Renouveler")').first();
      await btn.waitFor({ state: 'visible', timeout: 15000 });
      await btn.click();
      await page.waitForTimeout(4000);
      await page.waitForSelector('text=Temps restant', { timeout: 30000 });
      await saveScreenshot(page, 'debug-after-renew.png');

      const newText  = await getRemainText(page);
      const newHours = parseHours(newText);
      const newDays  = Math.floor(newHours / 24);
      const newHrs   = Math.floor(newHours % 24);
      console.log('[续期后]', newText);

      await tgNotify(
        '✅ <b>ACLClouds 续期成功</b>\n\n' +
        '🖥 服务器: <code>' + SERVER_ID + '</code>\n' +
        '⏰ 续期前: ' + remainText.trim() + '\n' +
        '🎉 续期后: <b>' + newDays + ' 天 ' + newHrs + ' 小时</b>\n\n' +
        '🕐 ' + new Date().toISOString()
      );
    } else {
      const d = Math.floor(remainHours / 24);
      const h = Math.floor(remainHours % 24);
      console.log('[跳过] 剩余', d, '天', h, '小时，无需续期');
      await tgNotify(
        'ℹ️ <b>ACLClouds 无需续期</b>\n\n' +
        '🖥 服务器: <code>' + SERVER_ID + '</code>\n' +
        '⏰ 当前剩余: <b>' + d + ' 天 ' + h + ' 小时</b>（大于 1 天，跳过）\n\n' +
        '🕐 ' + new Date().toISOString()
      );
    }

  } catch (err) {
    console.error('[错误]', err.message);
    if (browser) {
      try {
        const pg = browser.contexts()[0]?.pages()?.[0];
        if (pg) await saveScreenshot(pg, 'error-screenshot.png');
      } catch (_) {}
    }
    await tgNotify(
      '❌ <b>ACLClouds 续期失败</b>\n\n' +
      '🖥 服务器: <code>' + (SERVER_ID || '未设置') + '</code>\n' +
      '💥 ' + err.message + '\n\n' +
      '🕐 ' + new Date().toISOString()
    );
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
