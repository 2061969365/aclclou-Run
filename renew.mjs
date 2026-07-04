import { chromium } from 'playwright';

const EMAIL     = process.env.ACL_EMAIL;
const PASSWORD  = process.env.ACL_PASSWORD;
const SERVER_ID = process.env.ACL_SERVER_ID;
const TG_TOKEN  = process.env.TG_BOT_TOKEN;
const TG_CHAT   = process.env.TG_CHAT_ID;
const PROXY_SRV = 'socks5://127.0.0.1:1080';
const BASE_URL  = 'https://dash.aclclouds.com';

// 续期阈值(小时)：剩余时间 <= 此值才续期，> 此值则跳过
const RENEW_THRESHOLD_HOURS = 48; // 2 天

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
  const days  = parseInt((t.match(/(\d+)\s*[jd](?!\w)/) || [])[1] || '0', 10);
  const hours = parseInt((t.match(/(\d+)\s*h/)           || [])[1] || '0', 10);
  const mins  = parseInt((t.match(/(\d+)\s*min/)         || [])[1] || '0', 10);
  return days * 24 + hours + mins / 60;
}

function extractTimeStr(raw) {
  let m = raw.match(/\d+\s*[jd]\s*\d+\s*h\s*\d+\s*min/);
  if (m) return m[0];
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

// 从页面提取剩余时间文字
async function findTimeText(page) {
  return await page.evaluate(() => {
    const keywords = ['Time remaining', 'Temps restant'];
    const allEls = Array.from(document.querySelectorAll('p, span, div, h1, h2, h3, h4'));
    for (const el of allEls) {
      if (el.children.length > 3) continue;
      const text = (el.textContent || '').trim();
      for (const kw of keywords) {
        if (text.startsWith(kw) && text.match(/\d+\s*[hjd]/)) {
          return text;
        }
      }
    }
    return null;
  });
}

// 读取顶部电源状态徽章文字（精确匹配 "Offline" / "Online" 等，
// 与右侧 "STATUS / UPTIME" 面板里的大写 "OFFLINE" 区分开）
async function findPowerStatus(page) {
  return await page.evaluate(() => {
    const knownStates = ['Offline', 'Online', 'Running', 'Starting', 'Stopping', 'Restarting'];
    const allEls = Array.from(document.querySelectorAll('span, div, p, button'));
    for (const el of allEls) {
      if (el.children.length > 0) continue; // 只看纯文本叶子节点
      const text = (el.textContent || '').trim();
      if (knownStates.includes(text)) {
        return text;
      }
    }
    return null;
  });
}

// 检测到 Offline 时点击 Start 按钮
async function clickStartButton(page) {
  return await page.evaluate(() => {
    const btn = document.querySelector('button.power-btn[data-variant="start"]');
    if (btn) {
      btn.click();
      return true;
    }
    // 兜底：按文字找
    const buttons = Array.from(document.querySelectorAll('button'));
    const fallback = buttons.find(b => (b.textContent || '').trim() === 'Start');
    if (fallback) {
      fallback.click();
      return true;
    }
    return false;
  });
}

// 检查容器电源状态，如果是 Offline 则点击开机，并发送通知
async function checkAndStartServer(page) {
  console.log('[开机检测] 读取当前电源状态...');
  const status = await findPowerStatus(page);
  console.log('[开机检测] 当前状态:', status);

  if (status !== 'Offline') {
    console.log('[开机检测] 状态不是 Offline，跳过开机操作');
    return;
  }

  console.log('[开机检测] 检测到 Offline，点击 Start 按钮...');
  const clicked = await clickStartButton(page);
  if (!clicked) {
    console.log('[开机检测] 未找到 Start 按钮，放弃开机');
    await tgNotify(
      'ACLClouds 开机失败\n\n' +
      '服务器: ' + SERVER_ID + '\n' +
      '原因: 未找到 Start 按钮\n\n' +
      '时间: ' + new Date().toISOString()
    );
    return;
  }

  // 等待 2~3 秒后再检查状态
  await page.waitForTimeout(randInt(2000, 3000));
  const newStatus = await findPowerStatus(page);
  console.log('[开机检测] 点击后状态:', newStatus);

  if (newStatus !== 'Offline') {
    await tgNotify(
      'ACLClouds 开机成功\n\n' +
      '服务器: ' + SERVER_ID + '\n' +
      '当前状态: ' + (newStatus || '未知') + '\n\n' +
      '时间: ' + new Date().toISOString()
    );
  } else {
    await tgNotify(
      'ACLClouds 开机失败\n\n' +
      '服务器: ' + SERVER_ID + '\n' +
      '点击 Start 后仍为 Offline\n\n' +
      '时间: ' + new Date().toISOString()
    );
  }
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

    // ── Step 1: 登录页 ──
    console.log('[1] 打开登录页...');
    await page.goto(BASE_URL + '/auth/login', { waitUntil: 'networkidle', timeout: 60000 });
    await saveScreenshot(page, 'debug-login.png');

    // ── Step 2: 填邮箱密码 ──
    console.log('[2] 填写邮箱密码...');
    await page.waitForSelector('input[type="email"], #username', { timeout: 30000 });

    const emailInput = page.locator('input[type="email"], #username').first();
    await emailInput.click();
    await page.keyboard.type(EMAIL, { delay: randInt(50, 120) });

    const pwdInput = page.locator('input[type="password"], #password').first();
    await pwdInput.click();
    await page.keyboard.type(PASSWORD, { delay: randInt(50, 120) });

    // ── Step 3: 模拟 UI 操作处理 Captcha ──
    console.log('[3] 尝试点击人机验证...');
    const captchaBox = page.locator('text="I am not a robot"');
    if (await captchaBox.count() > 0) {
        // 模拟鼠标移动到验证码区域中心再点击
        const box = await captchaBox.first().boundingBox();
        if (box) {
            await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 10 });
            await page.waitForTimeout(randInt(200, 400));
            await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        } else {
            await captchaBox.first().click();
        }
        console.log('[Captcha] 已点击，等待网页自动验证...');
        // 等待网页内置 JS 运行并获取验证 Token
        await page.waitForTimeout(randInt(3500, 5000));
    } else {
        console.log('[Captcha] 未找到验证码复选框，尝试直接登录');
    }

    // ── Step 4: 点击 Sign in 按钮 ──
    console.log('[4] 点击登录...');
    await page.locator('button:has-text("Sign in")').click();

    // 等待登录完成跳转
    console.log('[5] 等待登录响应跳转...');
    try {
        await page.waitForNavigation({ waitUntil: 'networkidle', timeout: 15000 });
    } catch (e) {
        // 如果网络已空闲但没跳转，下方检查错误即可
    }

    // 检查是否有报错提示
    const pageText = await page.evaluate(() => document.body.innerText);
    if (pageText.includes('Captcha incorrect') || pageText.includes('These credentials do not match')) {
        throw new Error('登录失败: 验证码未通过或账号密码错误');
    }

    // ── 访问服务器页面 ──
    const serverUrl = BASE_URL + '/server/' + SERVER_ID;
    console.log('[->] 跳转到:', serverUrl);
    await page.goto(serverUrl, { waitUntil: 'networkidle', timeout: 60000 });
    if (page.url().includes('/auth/')) throw new Error('被重定向回登录页');
    await page.waitForTimeout(3000);
    await saveScreenshot(page, 'debug-server.png');

    // ── 新增：检测容器电源状态，如果 Offline 则尝试开机 ──
    await checkAndStartServer(page);

    // ── 等待并读取剩余时间 ──
    console.log('[等待] 查找剩余时间...');
    let remainRaw = null;
    for (let i = 0; i < 30; i++) {
      remainRaw = await findTimeText(page);
      if (remainRaw) { console.log('[找到]', remainRaw); break; }
      await page.waitForTimeout(1000);
    }
    if (!remainRaw) throw new Error('30秒内未找到剩余时间');

    const remainText  = extractTimeStr(remainRaw);
    const remainHours = parseHours(remainText);
    console.log('[时间]', remainText, '->', remainHours.toFixed(1), 'h');

    if (remainHours <= RENEW_THRESHOLD_HOURS) {
      console.log('[续期] 剩余 <= 2 天，点击续期...');

      // 点击续期按钮
      const btnText = await page.evaluate(() => {
        const keywords = ['Renouveler', 'Renew'];
        const buttons = Array.from(document.querySelectorAll('button, a'));
        for (const btn of buttons) {
          const t = (btn.textContent || '').trim();
          if (keywords.some(k => t === k || t.startsWith(k))) {
            btn.click();
            return t;
          }
        }
        return null;
      });
      if (!btnText) throw new Error('未找到续期按钮');
      console.log('[续期] 点击:', btnText);

      // 等待 "Renewing..." 状态消失
      console.log('[续期] 等待续期完成...');
      let newRemainRaw = null;
      for (let i = 0; i < 40; i++) {
        await page.waitForTimeout(1000);

        const isRenewing = await page.evaluate(() => {
          return document.body.innerText.includes('Renewing');
        });
        if (isRenewing) {
          console.log('[续期] 还在续期中... (' + (i + 1) + 's)');
          continue;
        }

        const t = await findTimeText(page);
        if (t) {
          const h = parseHours(extractTimeStr(t));
          if (h > remainHours + 1) {
            newRemainRaw = t;
            console.log('[续期] 完成，耗时', i + 1, '秒');
            break;
          }
        }
      }

      await saveScreenshot(page, 'debug-after-renew.png');

      if (!newRemainRaw) {
        console.log('[续期] 轮询超时，刷新页面读取最新时间...');
        await page.reload({ waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(2000);
        newRemainRaw = await findTimeText(page);
        await saveScreenshot(page, 'debug-after-renew.png');
      }

      const newText  = newRemainRaw ? extractTimeStr(newRemainRaw) : '未知';
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
        '当前剩余: ' + d + ' 天 ' + h + ' 小时（大于 2 天，跳过）\n\n' +
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
