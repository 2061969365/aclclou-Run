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

// 解析多种语言格式的剩余时间 → 总小时数
// 支持: "4j 23h 42min" / "23h 45min 15s" / "4d 23h 42min" / "4h 24min"
function parseHours(text) {
  const t = text || '';
  // 天：j（法语）或 d（英语）
  const days  = parseInt((t.match(/(\d+)\s*[jd](?!\w)/) || [])[1] || '0', 10);
  const hours = parseInt((t.match(/(\d+)\s*h/)           || [])[1] || '0', 10);
  const mins  = parseInt((t.match(/(\d+)\s*min/)         || [])[1] || '0', 10);
  return days * 24 + hours + mins / 60;
}

// 从原始文本提取时间部分
function extractTimeStr(raw) {
  // 带天数：4j 23h 42min 或 4d 23h 42min
  let m = raw.match(/\d+\s*[jd]\s*\d+\s*h\s*\d+\s*min/);
  if (m) return m[0];
  // 只有小时分钟：4h 24min
  m = raw.match(/\d+\s*h\s*\d+\s*min/);
  if (m) return m[0];
  return raw.trim();
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

// 等待"剩余时间"元素出现（兼容法语/英语）
// 法语: "Temps restant"，英语: "Time remaining"
async function waitForTimeElement(page, timeout = 45000) {
  console.log('[等待] 查找剩余时间元素...');
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      // 同时尝试两种语言
      const el = await page.locator(
        'text=Temps restant, text=Time remaining'
      ).first().textContent({ timeout: 3000 });
      console.log('[找到] 时间元素:', el.trim());
      return el;
    } catch (_) {
      await page.waitForTimeout(1000);
    }
  }
  // 超时前截图帮助调试
  const pageText = await page.evaluate(() => document.body.innerText.slice(0, 500));
  console.log('[调试] 页面文字:', pageText);
  throw new Error('等待剩余时间元素超时（45s），页面可能未加载完成');
}

(async () => {
  console.log('[代理]', PROXY_SRV);
  let browser;

  try {
    browser = await chromium.launch({
      headless: true,
      proxy: { server: PROXY_SRV },
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
    });

    const ctx = await browser.newContext({
      userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
      viewport: { width: 1280, height: 800 },
      locale: 'zh-CN',
    });

    const page = await ctx.newPage();
    page.setDefaultTimeout(60000);

    // ── Step 1: 打开登录页，提取 CSRF token ──
    console.log('[1] 打开登录页...');
    await page.goto(BASE_URL + '/auth/login', { waitUntil: 'networkidle', timeout: 60000 });
    await saveScreenshot(page, 'debug-login.png');

    const csrfToken = await page.evaluate(() => {
      const meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) return meta.getAttribute('content');
      const input = document.querySelector('input[name="_token"]');
      if (input) return input.value;
      const match = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
      if (match) return decodeURIComponent(match[1]);
      return null;
    });
    console.log('[CSRF] token:', csrfToken ? csrfToken.slice(0, 20) + '...' : 'not found');
    if (!csrfToken) throw new Error('无法获取 CSRF token');

    // ── Step 2 & 3: 填邮箱、密码 ──
    console.log('[2] 填写邮箱密码...');
    await page.waitForSelector('#username', { timeout: 30000 });
    await page.locator('#username').click();
    await page.keyboard.type(EMAIL, { delay: randInt(50, 120) });
    await page.locator('#password').click();
    await page.keyboard.type(PASSWORD, { delay: randInt(50, 120) });

    // ── Step 4: 模拟鼠标 + POST /auth/captcha ──
    console.log('[4] 模拟鼠标移动...');
    await page.mouse.move(400, 300);
    await page.waitForTimeout(randInt(200, 400));
    await page.mouse.move(320, 370, { steps: 8 });
    await page.waitForTimeout(randInt(200, 400));
    await page.mouse.move(320, 595, { steps: 12 });
    await page.waitForTimeout(randInt(500, 1000));

    console.log('[4] POST /auth/captcha...');
    const captchaResult = await page.evaluate(async (opts) => {
      try {
        const r = await fetch('/auth/captcha', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': opts.csrf, 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'include',
          body: JSON.stringify({ mouse_movements: opts.movements, mouse_distance: opts.distance, clicks: opts.clicks, key_presses: opts.keys, elapsed_ms: opts.elapsed }),
        });
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('application/json')) return { error: 'not-json', status: r.status, preview: (await r.text()).slice(0, 150) };
        return await r.json();
      } catch (e) { return { error: e.message }; }
    }, { csrf: csrfToken, movements: randInt(25, 60), distance: randInt(400, 900), clicks: 1, keys: EMAIL.length + PASSWORD.length, elapsed: randInt(5000, 9000) });

    console.log('[Captcha结果]', JSON.stringify(captchaResult));
    if (captchaResult.error) throw new Error('captcha 异常: ' + JSON.stringify(captchaResult));

    const captchaToken  = captchaResult.token  || '';
    const captchaAnswer = captchaResult.answer  || '';

    // ── Step 5: POST /auth/login ──
    console.log('[5] POST /auth/login...');
    const loginResult = await page.evaluate(async (opts) => {
      try {
        const r = await fetch('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': opts.csrf, 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'include',
          body: JSON.stringify({ user: opts.email, password: opts.password, captcha_token: opts.token, captcha_answer: opts.answer }),
        });
        const ct = r.headers.get('content-type') || '';
        const body = ct.includes('application/json') ? await r.json() : await r.text();
        return { status: r.status, body };
      } catch (e) { return { error: e.message }; }
    }, { csrf: csrfToken, email: EMAIL, password: PASSWORD, token: captchaToken, answer: captchaAnswer });

    console.log('[登录结果] status:', loginResult.status);
    if (loginResult.error) throw new Error('登录接口异常: ' + loginResult.error);
    if (loginResult.status !== 200 && loginResult.status !== 201) {
      await saveScreenshot(page, 'debug-login-failed.png');
      throw new Error('登录失败 ' + loginResult.status + ': ' + JSON.stringify(loginResult.body).slice(0, 150));
    }

    // ── 访问服务器控制台 ──
    const serverUrl = BASE_URL + '/server/' + SERVER_ID;
    console.log('[->] 跳转到:', serverUrl);
    await page.goto(serverUrl, { waitUntil: 'networkidle', timeout: 60000 });
    if (page.url().includes('/auth/')) throw new Error('被重定向回登录页，session 未生效');

    // 额外等待 3 秒让异步数据加载
    await page.waitForTimeout(3000);
    await saveScreenshot(page, 'debug-server.png');

    // ── 等待剩余时间元素（兼容英语/法语）──
    const rawTimeText = await waitForTimeElement(page);
    const remainText  = extractTimeStr(rawTimeText);
    const remainHours = parseHours(remainText);
    console.log('[时间] 提取:', remainText, '->', remainHours.toFixed(1), 'h');

    // ── 续期判断 ──
    if (remainHours <= 24) {
      console.log('[续期] 剩余 <= 1 天，点击续期按钮...');

      // 兼容英语 "Renew" 和法语 "Renouveler"
      const btn = page.locator('button:has-text("Renouveler"), button:has-text("Renew"), a:has-text("Renouveler"), a:has-text("Renew")').first();
      await btn.waitFor({ state: 'visible', timeout: 15000 });

      // 记录续期前的时间文字
      const oldRaw = await page.locator('text=Temps restant, text=Time remaining').first().textContent();
      console.log('[续期前]', oldRaw.trim());

      await btn.click();
      console.log('[续期] 已点击，等待页面更新...');

      // 等待时间文字变化（最多 25 秒）
      let newRaw = oldRaw;
      for (let i = 0; i < 25; i++) {
        await page.waitForTimeout(1000);
        try {
          newRaw = await page.locator('text=Temps restant, text=Time remaining').first().textContent();
          if (newRaw.trim() !== oldRaw.trim()) {
            console.log('[续期] 页面已更新，耗时', i + 1, '秒');
            break;
          }
        } catch (_) {}
      }

      await saveScreenshot(page, 'debug-after-renew.png');
      console.log('[续期后原文]', newRaw.trim());

      const newText  = extractTimeStr(newRaw);
      const newHours = parseHours(newText);
      const newDays  = Math.floor(newHours / 24);
      const newHrs   = Math.floor(newHours % 24);
      console.log('[续期后]', newText, '->', newHours.toFixed(1), 'h');

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
