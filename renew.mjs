import { chromium } from 'playwright';

const EMAIL     = process.env.ACL_EMAIL;
const PASSWORD  = process.env.ACL_PASSWORD;
const SERVER_ID = process.env.ACL_SERVER_ID;
const TG_TOKEN  = process.env.TG_BOT_TOKEN;
const TG_CHAT   = process.env.TG_CHAT_ID;
const PROXY_SRV = 'socks5://127.0.0.1:1080';

async function tgNotify(msg) {
  if (!TG_TOKEN || !TG_CHAT) { console.log('[TG] 未配置，跳过'); return; }
  try {
    const res = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: TG_CHAT, text: msg, parse_mode: 'HTML' }),
    });
    const d = await res.json();
    console.log(d.ok ? '[TG] 已发送' : '[TG] 失败: ' + d.description);
  } catch (e) {
    console.error('[TG] 异常:', e.message);
  }
}

// 解析 "2j 15h 48min" → 总小时数
function parseHours(text) {
  const t = text || '';
  const days  = parseInt((t.match(/(\d+)\s*j/)  || [])[1] || '0', 10);
  const hours = parseInt((t.match(/(\d+)\s*h/)  || [])[1] || '0', 10);
  const mins  = parseInt((t.match(/(\d+)\s*min/)|| [])[1] || '0', 10);
  return days * 24 + hours + mins / 60;
}

// 从页面里提取 "Temps restant" 区块的时间文本
async function getRemainText(page) {
  // 找包含 "Temps restant" 的元素，取其父容器的完整文字
  const el = page.locator('text=Temps restant').first();
  const raw = await el.textContent();
  console.log('[时间] 原始文本:', raw);
  const m = raw.match(/\d+\s*j\s*\d+\s*h\s*\d+\s*min/);
  return m ? m[0] : raw;
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
    page.setDefaultTimeout(30000);

    // ── Step 1: 打开登录页 ──
    console.log('[1/5] 打开登录页...');
    await page.goto('https://dash.aclclouds.com/auth/login', {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });

    // ── Step 2: 填邮箱 ──
    console.log('[2/5] 填写邮箱...');
    await page.waitForSelector('input[type="email"]', { timeout: 15000 });
    await page.locator('input[type="email"]').first().fill(EMAIL);

    // ── Step 3: 填密码 ──
    console.log('[3/5] 填写密码...');
    await page.locator('input[type="password"]').first().fill(PASSWORD);

    // ── Step 4: 勾选人机验证 checkbox ──
    console.log('[4/5] 勾选人机验证...');
    const cb = page.locator('input[type="checkbox"]').first();
    await cb.waitFor({ state: 'visible', timeout: 10000 });
    await cb.click();
    await page.waitForTimeout(800);

    // ── Step 5: 点击 Sign in ──
    console.log('[5/5] 点击登录...');
    await page.locator('button:has-text("Sign in"), button[type="submit"]').first().click();
    await page.waitForURL(/dash\.aclclouds\.com\/(?!auth)/, { timeout: 20000 });
    console.log('[✓] 登录成功:', page.url());

    // ── 访问服务器控制台 ──
    const serverUrl = 'https://dash.aclclouds.com/server/' + SERVER_ID;
    console.log('[→] 访问:', serverUrl);
    await page.goto(serverUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('text=Temps restant', { timeout: 15000 });

    const remainText = await getRemainText(page);
    const remainHours = parseHours(remainText);
    console.log('[时间] 解析结果:', remainText, '→', remainHours.toFixed(1), '小时');

    // ── 续期判断 ──
    if (remainHours <= 24) {
      console.log('[续期] 剩余 ≤ 1 天，点击 Renouveler...');
      const btn = page.locator('button:has-text("Renouveler"), a:has-text("Renouveler")').first();
      await btn.waitFor({ state: 'visible', timeout: 10000 });
      await btn.click();

      await page.waitForTimeout(4000);
      await page.waitForSelector('text=Temps restant', { timeout: 15000 });

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
        if (pg) await pg.screenshot({ path: 'error-screenshot.png', fullPage: true });
        console.log('[截图] 已保存 error-screenshot.png');
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
