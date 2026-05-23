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

    // ── 先用 API 登录，拦截 /api/auth/login 的请求和响应 ──
    // 打开登录页，监听网络请求，找到真实的登录接口
    const page = await ctx.newPage();
    page.setDefaultTimeout(60000);

    // 拦截所有 XHR/fetch，记录登录接口的细节
    let loginApiUrl   = null;
    let loginApiBody  = null;
    let loginApiCtype = null;

    page.on('request', req => {
      const url = req.url();
      const method = req.method();
      // 捕获所有 POST 到 auth/login 相关路径的请求
      if (method === 'POST' && (url.includes('login') || url.includes('auth'))) {
        loginApiUrl   = url;
        loginApiBody  = req.postData();
        loginApiCtype = req.headers()['content-type'] || '';
        console.log('[拦截] POST', url);
        console.log('[拦截] Content-Type:', loginApiCtype);
        console.log('[拦截] Body:', loginApiBody);
      }
    });

    page.on('response', async resp => {
      const url = resp.url();
      if (url.includes('login') || url.includes('auth')) {
        try {
          const body = await resp.text();
          console.log('[拦截] Response', resp.status(), url, body.slice(0, 300));
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

    // ── Step 4: 勾选 checkbox（用 JS 直接设置 aria-checked 状态绕过服务端验证）──
    // DOM 显示: <div role="checkbox" aria-checked="false" class="auth-captcha-inner">
    // 策略：先正常点击，同时用 JS 强制设置 aria-checked="true" 欺骗前端校验
    console.log('[4] 处理人机验证...');
    await page.evaluate(() => {
      const cb = document.querySelector('[role="checkbox"]');
      if (cb) {
        // 强制设置已选中状态
        cb.setAttribute('aria-checked', 'true');
        cb.classList.add('checked');
        // 触发所有可能监听的事件
        ['click', 'change', 'input'].forEach(evt =>
          cb.dispatchEvent(new Event(evt, { bubbles: true }))
        );
        console.log('aria-checked set to true');
      }
    });
    await page.waitForTimeout(500);

    // 再尝试真实点击 checkbox 区域（让服务端也触发）
    try {
      await page.locator('.auth-captcha-inner').click({ timeout: 5000 });
      console.log('[4] 点击 auth-captcha-inner 成功');
    } catch (_) {
      try {
        await page.locator('.auth-captcha-checkbox').click({ timeout: 3000 });
        console.log('[4] 点击 auth-captcha-checkbox 成功');
      } catch (_2) {
        console.log('[4] 点击失败，继续');
      }
    }
    await page.waitForTimeout(1000);

    // 截图检查 checkbox 状态
    await saveScreenshot(page, 'debug-before-signin.png');

    // ── Step 5: 点击 Sign in，同时监听 API 请求 ──
    console.log('[5] 点击登录...');
    await page.locator('button:has-text("Sign in"), button[type="submit"]').first().click();

    // 等待跳转，最多 30 秒
    try {
      await page.waitForURL(/dash\.aclclouds\.com\/(?!auth)/, { timeout: 30000 });
      console.log('[✓] 浏览器登录成功:', page.url());
    } catch (navErr) {
      // 跳转失败，截图看看是什么错误
      await saveScreenshot(page, 'debug-login-failed.png');

      // 读取页面错误文字
      const errText = await page.evaluate(() => {
        const el = document.querySelector('.error, [class*="error"], [class*="alert"], [role="alert"]');
        return el ? el.textContent.trim() : '';
      });
      console.log('[登录失败] 页面错误:', errText);

      // 如果已经拦截到了登录 API，尝试直接用 API 登录
      if (loginApiUrl && loginApiBody) {
        console.log('[API] 尝试直接调用登录接口...');
        const apiResp = await page.evaluate(async (url, body, ctype) => {
          const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': ctype || 'application/json' },
            body: body,
            credentials: 'include',
          });
          return { status: r.status, body: await r.text() };
        }, loginApiUrl, loginApiBody, loginApiCtype);
        console.log('[API] 响应:', apiResp.status, apiResp.body.slice(0, 300));
      } else {
        console.log('[API] 未捕获到登录接口，无法重试');
        throw navErr;
      }

      // 再等一次跳转
      await page.waitForURL(/dash\.aclclouds\.com\/(?!auth)/, { timeout: 15000 });
      console.log('[✓] API 登录成功:', page.url());
    }

    // ── 访问服务器控制台 ──
    const serverUrl = BASE_URL + '/server/' + SERVER_ID;
    console.log('[→] 访问:', serverUrl);
    await page.goto(serverUrl, { waitUntil: 'networkidle', timeout: 60000 });
    await saveScreenshot(page, 'debug-server.png');

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
