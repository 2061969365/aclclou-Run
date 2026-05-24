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
      body: JSON.stringify({ chat_id: TG_CHAT, text: msg }),
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

    // ── Step 1: 打开登录页，建立 session 并提取 CSRF token ──
    console.log('[1] 打开登录页...');
    await page.goto(BASE_URL + '/auth/login', { waitUntil: 'networkidle', timeout: 60000 });
    await saveScreenshot(page, 'debug-login.png');

    // 从页面提取 CSRF token（Laravel 通常放在 <meta name="csrf-token"> 或隐藏 input）
    const csrfToken = await page.evaluate(() => {
      // 方式1: <meta name="csrf-token" content="...">
      const meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) return meta.getAttribute('content');
      // 方式2: <input name="_token" value="...">
      const input = document.querySelector('input[name="_token"]');
      if (input) return input.value;
      // 方式3: 从 cookie 读取 XSRF-TOKEN
      const match = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
      if (match) return decodeURIComponent(match[1]);
      return null;
    });
    console.log('[CSRF] token:', csrfToken ? csrfToken.slice(0, 20) + '...' : 'not found');

    if (!csrfToken) {
      // 打印页面 meta 标签和 cookie 帮助调试
      const debugInfo = await page.evaluate(() => ({
        metas: Array.from(document.querySelectorAll('meta')).map(m => ({
          name: m.name, content: (m.content || '').slice(0, 50)
        })),
        cookie: document.cookie.slice(0, 200),
      }));
      console.log('[调试] meta tags:', JSON.stringify(debugInfo.metas));
      console.log('[调试] cookies:', debugInfo.cookie);
      throw new Error('无法获取 CSRF token');
    }

    // ── Step 2 & 3: 填邮箱、密码 ──
    console.log('[2] 填写邮箱密码...');
    await page.waitForSelector('#username', { timeout: 30000 });
    await page.locator('#username').click();
    await page.keyboard.type(EMAIL, { delay: randInt(50, 120) });
    await page.locator('#password').click();
    await page.keyboard.type(PASSWORD, { delay: randInt(50, 120) });

    // ── Step 4: 模拟鼠标移动，POST /auth/captcha 带 CSRF token ──
    console.log('[4] 模拟鼠标移动...');
    await page.mouse.move(400, 300);
    await page.waitForTimeout(randInt(200, 400));
    await page.mouse.move(320, 370, { steps: 8 });
    await page.waitForTimeout(randInt(200, 400));
    await page.mouse.move(320, 595, { steps: 12 });
    await page.waitForTimeout(randInt(500, 1000));

    console.log('[4] POST /auth/captcha（带 CSRF token）...');
    const captchaResult = await page.evaluate(async (opts) => {
      const payload = {
        mouse_movements: opts.movements,
        mouse_distance:  opts.distance,
        clicks:          opts.clicks,
        key_presses:     opts.keys,
        elapsed_ms:      opts.elapsed,
      };
      try {
        const r = await fetch('/auth/captcha', {
          method: 'POST',
          headers: {
            'Content-Type':     'application/json',
            'X-CSRF-TOKEN':     opts.csrf,
            'X-Requested-With': 'XMLHttpRequest',
          },
          credentials: 'include',
          body: JSON.stringify(payload),
        });
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('application/json')) {
          const text = await r.text();
          return { error: 'not-json', status: r.status, preview: text.slice(0, 150) };
        }
        return await r.json();
      } catch (e) {
        return { error: e.message };
      }
    }, {
      csrf:      csrfToken,
      movements: randInt(25, 60),
      distance:  randInt(400, 900),
      clicks:    1,
      keys:      EMAIL.length + PASSWORD.length,
      elapsed:   randInt(5000, 9000),
    });

    console.log('[Captcha结果]', JSON.stringify(captchaResult));

    if (captchaResult.error) {
      throw new Error('captcha 接口异常: ' + JSON.stringify(captchaResult));
    }

    const captchaToken  = captchaResult.token  || '';
    const captchaAnswer = captchaResult.answer  || '';
    console.log('[Captcha] passed:', captchaResult.passed, '| token:', captchaToken, '| answer:', captchaAnswer);

    // ── Step 5: POST /auth/login 带 CSRF token + captcha token ──
    console.log('[5] POST /auth/login...');
    const loginResult = await page.evaluate(async (opts) => {
      try {
        const r = await fetch('/auth/login', {
          method: 'POST',
          headers: {
            'Content-Type':     'application/json',
            'X-CSRF-TOKEN':     opts.csrf,
            'X-Requested-With': 'XMLHttpRequest',
          },
          credentials: 'include',
          body: JSON.stringify({
            user:           opts.email,
            password:       opts.password,
            captcha_token:  opts.token,
            captcha_answer: opts.answer,
          }),
        });
        const ct = r.headers.get('content-type') || '';
        const body = ct.includes('application/json') ? await r.json() : await r.text();
        return { status: r.status, body };
      } catch (e) {
        return { error: e.message };
      }
    }, {
      csrf:     csrfToken,
      email:    EMAIL,
      password: PASSWORD,
      token:    captchaToken,
      answer:   captchaAnswer,
    });

    console.log('[登录结果] status:', loginResult.status, '| body:', JSON.stringify(loginResult.body).slice(0, 200));

    if (loginResult.error) {
      throw new Error('登录接口异常: ' + loginResult.error);
    }
    if (loginResult.status !== 200 && loginResult.status !== 201) {
      await saveScreenshot(page, 'debug-login-failed.png');
      const detail = typeof loginResult.body === 'object'
        ? JSON.stringify(loginResult.body)
        : String(loginResult.body).slice(0, 150);
      throw new Error('登录失败 ' + loginResult.status + ': ' + detail);
    }

    // ── 登录成功，访问服务器控制台 ──
    const serverUrl = BASE_URL + '/server/' + SERVER_ID;
    console.log('[->] 跳转到:', serverUrl);
    await page.goto(serverUrl, { waitUntil: 'networkidle', timeout: 60000 });
    console.log('[当前URL]', page.url());

    if (page.url().includes('/auth/')) {
      await saveScreenshot(page, 'debug-redirected.png');
      throw new Error('被重定向回登录页，session 未生效');
    }

    await saveScreenshot(page, 'debug-server.png');

    // ── 读取剩余时间 ──
    await page.waitForSelector('text=Temps restant', { timeout: 30000 });
    const remainText  = await getRemainText(page);
    const remainHours = parseHours(remainText);
    console.log('[时间]', remainText, '->', remainHours.toFixed(1), 'h');

    // ── 续期判断 ──
    if (remainHours <= 24) {
      console.log('[续期] 剩余 <= 1 天，续期中...');
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
        'ACLClouds 续期成功\n\n' +
        '服务器: ' + SERVER_ID + '\n' +
        '续期前: ' + remainText.trim() + '\n' +
        '续期后: ' + newDays + ' 天 ' + newHrs + ' 小时\n\n' +
        '时间: ' + new Date().toISOString()
      );
    } else {
      const d = Math.floor(remainHours / 24);
      const h = Math.floor(remainHours % 24);
      console.log('[跳过] 剩余', d, '天', h, '小时，无需续期');
      await tgNotify(
        'ACLClouds 无需续期\n\n' +
        '服务器: ' + SERVER_ID + '\n' +
        '当前剩余: ' + d + ' 天 ' + h + ' 小时（大于 1 天，跳过）\n\n' +
        '时间: ' + new Date().toISOString()
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
      'ACLClouds 续期失败\n\n' +
      '服务器: ' + (SERVER_ID || '未设置') + '\n' +
      '错误: ' + err.message.slice(0, 200) + '\n\n' +
      '时间: ' + new Date().toISOString()
    );
    process.exit(1);
  } finally {
    if (browser) await browser.close();
  }
})();
