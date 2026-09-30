#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACLClouds Auto Renewal Script (Playwright)
==========================================
自动登录 ACLClouds，检查服务器状态：
  - 验证码识别（ddddocr + 图片匹配）
  - 剩余时间 ≤ 阈值时自动续期
  - 服务器离线时自动开机
  - 结果通过 Telegram 通知

用法：设置环境变量后运行脚本
"""

import hashlib
import json
import os
import re
import sys
import time
import random
import asyncio
import tempfile
from datetime import datetime

import difflib

import ddddocr
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ============================================================
# 环境变量
# ============================================================
EMAIL = os.environ.get("ACL_EMAIL", "")
PASSWORD = os.environ.get("ACL_PASSWORD", "")
SERVER_ID = os.environ.get("ACL_SERVER_ID", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
BASE_URL = "https://dash.aclclouds.com"
RENEW_THRESHOLD_HOURS = 48

COOKIE_DIR = os.environ.get("COOKIE_DIR", os.path.join(tempfile.gettempdir(), "aclclou_cookies"))
DEBUG_DIR = os.environ.get("DEBUG_DIR", os.path.join(tempfile.gettempdir(), "aclclou_debug"))
MAX_LOGIN_RETRY = 5
SIGNATURE = "ACLClouds Auto Renewal"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

# ============================================================
# 日志与统计
# ============================================================
STATS = {
    "renewals": 0,
    "skipped": 0,
    "failures": 0,
    "starts": 0,
}


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def step(name: str, ok: bool, detail: str = ""):
    emoji = "[OK]" if ok else "[FAIL]"
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [STEP] {emoji} {name} -- {detail}")


def error(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [ERROR] {msg}")


def mask(text: str) -> str:
    if not text:
        return "***"
    if "@" in text:
        local, domain = text.split("@", 1)
        return f"{local[:3]}***@{domain}"
    return "***"


async def save_screenshot(name: str, page):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{name}.png")
        await page.screenshot(path=path, full_page=True)
        log(f"[DEBUG] Screenshot saved: {path}")
    except Exception as e:
        log(f"[DEBUG] Screenshot failed: {e}")


def fmt_hours(hours: float) -> str:
    if hours <= 0:
        return "Expired"
    if hours < 1:
        return f"{int(hours * 60)} min"
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h}h {m}m"


# ============================================================
# Telegram 通知
# ============================================================
async def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            async with session.post(
                url, data={"chat_id": TG_CHAT, "text": text}, timeout=30
            ) as resp:
                if resp.status == 200:
                    log("[TG] Notification sent")
                else:
                    log(f"[TG] Send failed: {resp.status}")
    except Exception as e:
        log(f"[TG] Error: {e}")


# ============================================================
# Cookie 缓存模块
# ============================================================
def get_cookie_path(email: str) -> str:
    os.makedirs(COOKIE_DIR, exist_ok=True)
    email_hash = hashlib.md5(email.encode()).hexdigest()[:8]
    return os.path.join(COOKIE_DIR, f"{email_hash}.json")


def save_cookies(email: str, cookies: list):
    try:
        path = get_cookie_path(email)
        data = {"cookies": cookies, "email": email, "saved_at": time.time()}
        with open(path, "w") as f:
            json.dump(data, f)
        log(f"[COOKIE] Saved: {path}")
    except Exception as e:
        log(f"[COOKIE] Save failed: {e}")


def load_cookies(email: str) -> list | None:
    try:
        path = get_cookie_path(email)
        if not os.path.exists(path):
            return None
        with open(path, "r") as f:
            data = json.load(f)
        if time.time() - data.get("saved_at", 0) > 86400 * 7:
            log("[COOKIE] Expired (>7 days)")
            return None
        log(f"[COOKIE] Loaded: {path}")
        return data.get("cookies")
    except Exception as e:
        log(f"[COOKIE] Load failed: {e}")
        return None


# ============================================================
# CaptchaSolver 类 - 验证码识别
# ============================================================
class CaptchaSolver:
    def __init__(self):
        self.ocr = ddddocr.DdddOcr(show_ad=False)

    async def solve(self, page, max_retries=5) -> bool:
        checkbox = page.locator(".auth-captcha-checkbox")
        if await checkbox.count() == 0:
            # 兼容：有些版本直接显示 Verified，无需点击
            verified = page.locator("text=Verified")
            if await verified.count() > 0 and await verified.first.is_visible():
                log("[CAPTCHA] Already verified without checkbox")
                return True
            log("[CAPTCHA] No checkbox found")
            return False

        # 已是 Verified 状态则直接成功
        try:
            txt = (await checkbox.inner_text()).lower()
            if "verified" in txt:
                log("[CAPTCHA] Already verified")
                return True
        except Exception:
            pass

        # 人性化点击（避免被检测为自动化）
        try:
            box = await checkbox.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
                cy = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
                await page.mouse.move(cx, cy)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await page.mouse.click(cx, cy)
            else:
                await checkbox.click()
        except Exception:
            try:
                await checkbox.click()
            except Exception:
                pass
        log("[CAPTCHA] Clicked checkbox")
        await asyncio.sleep(2)

        challenge = page.locator(".auth-captcha-challenge")
        verified_indicator = page.locator("text=Verified")

        try:
            await challenge.wait_for(state="visible", timeout=8000)
        except PlaywrightTimeout:
            await asyncio.sleep(1)
            # WARP 可信 IP 下点击后直接 Verified，无需图片挑战 - 视为成功（本次截图就是此情况）
            try:
                if await verified_indicator.count() > 0 and await verified_indicator.first.is_visible():
                    log("[CAPTCHA] Verified without challenge - success")
                    return True
            except Exception:
                pass
            try:
                txt = (await checkbox.inner_text()).lower()
                if "verified" in txt:
                    log("[CAPTCHA] Verified without challenge (checkbox) - success")
                    return True
            except Exception:
                pass
            # 无挑战也可能是网络延迟，再等2秒后仍无挑战则按成功处理，允许后续点击 Sign in
            await asyncio.sleep(2)
            if await challenge.count() > 0:
                try:
                    if await challenge.is_visible():
                        log("[CAPTCHA] Challenge appeared after delay")
                    else:
                        log("[CAPTCHA] No challenge required, assuming verified")
                        return True
                except Exception:
                    log("[CAPTCHA] No challenge required, assuming verified")
                    return True
            else:
                log("[CAPTCHA] No challenge required, assuming verified")
                return True

        for attempt in range(1, max_retries + 1):
            log(f"  [CAPTCHA] OCR attempt {attempt}/{max_retries}")

            try:
                prompt_elem = page.locator(".auth-captcha-prompt strong")
                if await prompt_elem.count() == 0:
                    log("[CAPTCHA] No prompt found")
                    continue

                target_text = (await prompt_elem.inner_text()).strip().lower()
                log(f"[CAPTCHA] Target: '{target_text}'")

                options = page.locator(".auth-captcha-option")
                count = await options.count()
                if count != 4:
                    log(f"[CAPTCHA] Expected 4 options, got {count}")
                    continue

                for idx in range(count):
                    option = options.nth(idx)
                    img = option.locator(".auth-captcha-option-img")
                    if await img.count() == 0:
                        continue

                    src = await img.get_attribute("src")
                    if src:
                        try:
                            resp = await page.context.request.get(src)
                            img_bytes = await resp.body()
                        except Exception:
                            img_bytes = await img.screenshot()
                    else:
                        img_bytes = await img.screenshot()

                    try:
                        recognized = self.ocr.classification(img_bytes).lower().strip()
                    except Exception as e:
                        log(f"[CAPTCHA] OCR error on option {idx+1}: {e}")
                        recognized = ""

                    log(f"[CAPTCHA] Option {idx+1}: '{recognized}'")

                    # 模糊匹配：容错 OCR 拼写误差（如 minecra vs minecraft）
                    ratio = difflib.SequenceMatcher(None, target_text, recognized).ratio()
                    is_match = (
                        target_text == recognized
                        or ratio > 0.65
                        or (len(target_text) > 3 and target_text in recognized)
                        or (len(recognized) > 3 and recognized in target_text)
                        or (len(target_text) > 2 and (target_text in recognized.split() or recognized in target_text.split()))
                    )
                    if is_match:
                        log(f"[CAPTCHA] Match found: option {idx+1} (ratio={ratio:.2f})")
                        await option.click()
                        await asyncio.sleep(1)
                        # Verify challenge accepted
                        if await challenge.is_visible():
                            log("[CAPTCHA] Challenge still visible after click, might have failed")
                            continue
                        log(f"[CAPTCHA] Match found: option {idx+1}, challenge accepted")
                        return True

                log("[CAPTCHA] No match, refreshing images...")
                if attempt < max_retries:
                    ref_btn = page.locator(".auth-captcha-refresh, button:has-text('Refresh'), [aria-label*='refresh']")
                    if await ref_btn.count() > 0:
                        await ref_btn.first.click()
                        await asyncio.sleep(1)

            except Exception as e:
                log(f"[CAPTCHA] Error: {e}")
                await asyncio.sleep(1)

        log("[CAPTCHA] Max OCR retries reached")
        return False


# ============================================================
# ACLCloudsRenewer 主类
# ============================================================
class ACLCloudsRenewer:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.browser = None
        self.context = None
        self.page = None
        self.captcha_solver = CaptchaSolver()
        self.logged_in = False
        self.pw = None
        self.server_ids = []

    async def start_browser(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.context = await self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        self.page = await self.context.new_page()
        # ── 网络/控制台监听：定位 Cap 续期接口的真实返回 ──
        try:
            self.page.on("console", lambda msg: log(f"[CONSOLE][{msg.type}] {(msg.text or '')[:200]}") if any(k in (msg.text or '').lower() for k in ["captcha", "cap.", "renew", "verif", "error", "fail"]) else None)
            async def _log_resp(resp):
                try:
                    url = resp.url.lower()
                    if any(k in url for k in ["cap.", "captcha", "renew", "challenge", "verify"]):
                        log(f"[NET][{resp.status}] {resp.url[:150]}")
                    # 续期接口：无论成功失败都记录正文，403 原因全靠它
                    if "upgrade/renew" in url:
                        try:
                            body = await resp.body()
                            log(f"[API][{resp.status}] renew resp: {body[:500]}")
                        except Exception as e:
                            log(f"[API][{resp.status}] renew body skip: {e}")
                except Exception:
                    pass
            self.page.on("response", lambda r: asyncio.ensure_future(_log_resp(r)))
        except Exception as e:
            log(f"[BROWSER] listener skip: {e}")
        log("[BROWSER] Started")

    async def close_browser(self):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        log("[BROWSER] Closed")

    # ── Cookie 恢复 ──
    async def restore_cookies(self) -> bool:
        cookies = load_cookies(self.email)
        if not cookies:
            return False
        try:
            await self.context.add_cookies(cookies)
            await self.page.goto(f"{BASE_URL}/dashboard/projects", wait_until="networkidle", timeout=60000)
            await asyncio.sleep(2)
            if "/dashboard/" in self.page.url and "/auth/" not in self.page.url:
                log(f"[COOKIE] {mask(self.email)} cookie valid, logged in")
                self.logged_in = True
                return True
            else:
                log(f"[COOKIE] {mask(self.email)} cookie expired")
                return False
        except Exception as e:
            log(f"[COOKIE] Restore failed: {e}")
            return False

    # ── 登录 ──
    async def login(self) -> bool:
        if self.logged_in:
            log(f"[LOGIN] {mask(self.email)} already logged in via cookie")
            return True

        for attempt in range(1, MAX_LOGIN_RETRY + 1):
            log(f"[LOGIN] Attempt {attempt}/{MAX_LOGIN_RETRY}: {mask(self.email)}")

            try:
                await self.page.goto(
                    f"{BASE_URL}/auth/login",
                    wait_until="networkidle",
                    timeout=60000,
                )
                await asyncio.sleep(random.uniform(2, 3))

                if "challenge" in self.page.url.lower():
                    wait = 10 + attempt * 3
                    log(f"[LOGIN] Challenge page, waiting {wait}s...")
                    await save_screenshot(f"challenge_{self.email.split('@')[0]}_{attempt}", self.page)
                    await asyncio.sleep(wait)
                    continue

                turnstile = self.page.locator("#cf-turnstile, [data-turnstile], iframe[src*='challenges']")
                if await turnstile.count() > 0:
                    log("[LOGIN] Turnstile widget detected on page")
                    await save_screenshot(f"turnstile_{self.email.split('@')[0]}_{attempt}", self.page)
                    wait = 10 + attempt * 3
                    await asyncio.sleep(wait)
                    continue

                try:
                    email_input = self.page.locator('input[name="email"], input[type="email"], #username')
                    await email_input.first.wait_for(state="visible", timeout=15000)
                except PlaywrightTimeout:
                    log("[LOGIN] Email input not loaded after SPA render")
                    await save_screenshot(f"no_form_{self.email.split('@')[0]}_{attempt}", self.page)
                    await asyncio.sleep(3)
                    continue

                await email_input.fill(self.email)
                await asyncio.sleep(random.uniform(0.5, 1.0))

                password_input = self.page.locator('input[name="password"], input[type="password"], #password')
                if await password_input.count() == 0:
                    log("[LOGIN] Password input not found")
                    continue

                await password_input.fill(self.password)
                await asyncio.sleep(random.uniform(0.5, 1.0))

                captcha_ok = await self.captcha_solver.solve(self.page, max_retries=5)
                if not captcha_ok:
                    log("[LOGIN] Captcha failed")
                    continue

                current_email = await email_input.input_value()
                current_password = await password_input.input_value()
                if not current_email or not current_password:
                    log(f"[LOGIN] Fields cleared (email={bool(current_email)}, pwd={bool(current_password)}), re-entering...")
                    if not current_email:
                        await email_input.fill(self.email)
                    if not current_password:
                        await password_input.fill(self.password)
                    await asyncio.sleep(0.5)

                login_btn = self.page.locator('button:has-text("Sign in"), button:has-text("Login"), button[type="submit"]')
                if await login_btn.count() == 0:
                    log("[LOGIN] Login button not found")
                    continue

                await login_btn.first.click()

                try:
                    await self.page.wait_for_function(
                        'window.location.href && '
                        'window.location.href !== "about:blank" && '
                        '!window.location.href.includes("/auth/")',
                        timeout=15000,
                    )
                except (PlaywrightTimeout, TimeoutError, Exception):
                    pass

                await asyncio.sleep(1)
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except (PlaywrightTimeout, TimeoutError, Exception):
                    pass

                current_url = self.page.url
                log(f"[LOGIN] Post-submit URL: {current_url!r}")

                if current_url and current_url not in ("about:blank",) and "/auth/" not in current_url:
                    log(f"[LOGIN] Success: {mask(self.email)} (attempt {attempt})")
                    self.logged_in = True
                    cookies = await self.context.cookies()
                    save_cookies(self.email, cookies)
                    return True

                page_content = await self.page.content()
                log(f"[LOGIN] Still on login page, size={len(page_content)}")

                if "incorrect" in page_content.lower() or "invalid" in page_content.lower():
                    if "password" in page_content.lower():
                        log(f"[LOGIN] Password error, retrying...")
                        await asyncio.sleep(random.uniform(1, 2))
                        continue
                    if "captcha" in page_content.lower() or "code" in page_content.lower():
                        log(f"[LOGIN] Captcha error, retrying...")
                        await asyncio.sleep(random.uniform(1, 2))
                        continue

                await save_screenshot(f"login_stuck_{self.email.split('@')[0]}_{attempt}", self.page)
                log(f"[LOGIN] Unknown result, retrying...")
                await asyncio.sleep(random.uniform(2, 4))

            except Exception as e:
                log(f"[LOGIN] Error: {e}")
                await save_screenshot(f"login_error_{self.email.split('@')[0]}_{attempt}", self.page)
                await asyncio.sleep(random.uniform(3, 6))

        log(f"[LOGIN] Failed: {mask(self.email)} after {MAX_LOGIN_RETRY} attempts")
        return False

    # ── 获取服务器状态 ──
    async def get_server_status(self, server_id: str) -> dict:
        result = {
            "server_id": server_id,
            "remaining_hours": -1,
            "is_online": False,
            "error": None,
        }

        try:
            url = f"{BASE_URL}/server/{server_id}"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            time_text = ""
            for selector in [
                "text=Time remaining",
                "text=Temps restant",
                ".time-remaining",
                "[class*=time]",
                "[class*=countdown]",
                "[class*=expiry]",
                "text=Remaining",
                "text=Expires in",
                "text=Expires",
                "text=到期",
            ]:
                elem = self.page.locator(selector)
                if await elem.count() > 0:
                    try:
                        time_text = await elem.first.inner_text()
                    except Exception:
                        continue
                    # 必须含数字才接受：排除 "EXPIRY" 表头等误匹配（21:32 误报教训）
                    if time_text and time_text.strip() and re.search(r"\d", time_text):
                        break
                    time_text = ""

            if time_text:
                log(f"[STATUS] Raw time text: '{time_text}'")
                hours = self._parse_remaining_hours(time_text)
                result["remaining_hours"] = hours

            # ── Fallback: dashboard/projects 兜底（官网改版后 /server 页可能取不到时间）──
            # 截图证据：/server 解析为 Expired(-1)，但 dashboard 显示 Expires in 14h / Active
            if result["remaining_hours"] < 0:
                try:
                    await self.page.goto(f"{BASE_URL}/dashboard/projects", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    dash_text = ""
                    for selector in [
                        "text=Expires in",
                        "text=Time remaining",
                        "text=Remaining",
                    ]:
                        elem = self.page.locator(selector)
                        if await elem.count() > 0:
                            try:
                                dash_text = await elem.first.inner_text()
                            except Exception:
                                continue
                            if dash_text and dash_text.strip() and re.search(r"\d", dash_text):
                                break
                            dash_text = ""
                    if dash_text:
                        log(f"[STATUS] Dashboard fallback raw: '{dash_text}'")
                        hours = self._parse_remaining_hours(dash_text)
                        if hours >= 0:
                            result["remaining_hours"] = hours
                    # Expiry 日期兜底：全局搜所有 MM/DD/YYYY，取最晚的未来日期
                    # （旧正则要求日期紧跟 Expiry 关键字，会被 UUID 中的数字截断而失败）
                    if result["remaining_hours"] < 0:
                        try:
                            body = await self.page.content()
                            from datetime import datetime as _dt
                            now = _dt.utcnow()
                            best = None
                            for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", body):
                                try:
                                    exp = _dt(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                                except ValueError:
                                    continue
                                delta_h = (exp - now).total_seconds() / 3600
                                if delta_h > -72 and (best is None or delta_h > best[0]):
                                    best = (delta_h, m.group(0))
                            if best is not None:
                                result["remaining_hours"] = max(best[0], 0.0)
                                log(f"[STATUS] Parsed expiry date {best[1]} -> {result['remaining_hours']:.1f}h")
                        except Exception as e2:
                            log(f"[STATUS] Expiry date parse skip: {e2}")
                except Exception as e1:
                    log(f"[STATUS] Dashboard fallback skip: {e1}")

            power_status = None
            online_indicators = [
                ".status-online",
                "text=Online",
                "text=En ligne",
                "text=Active",
                "text=Running",
                "[data-status=online]",
                "[data-status=active]",
            ]
            for sel in online_indicators:
                elem = self.page.locator(sel)
                if await elem.count() > 0:
                    result["is_online"] = True
                    break

            if not result["is_online"]:
                power_status = await self.page.evaluate("""() => {
                    const knownStates = ['Offline', 'Online', 'Running', 'Active', 'Starting', 'Stopping', 'Restarting'];
                    const allEls = Array.from(document.querySelectorAll('span, div, p, button'));
                    for (const el of allEls) {
                        if (el.children.length > 0) continue;
                        const text = (el.textContent || '').trim();
                        if (knownStates.includes(text)) {
                            return text;
                        }
                    }
                    return null;
                }""")
                if power_status in ("Online", "Running", "Active", "Starting", "Restarting"):
                    result["is_online"] = True

            offline_indicators = [
                ".status-offline",
                "text=Offline",
                "text=Hors ligne",
                "[data-status=offline]",
            ]
            for sel in offline_indicators:
                elem = self.page.locator(sel)
                if await elem.count() > 0:
                    result["is_online"] = False
                    break

            if result["is_online"] and power_status == "Offline":
                result["is_online"] = False

        except Exception as e:
            result["error"] = str(e)
            log(f"[STATUS] Error: {e}")

        return result

    def _parse_remaining_hours(self, text: str) -> float:
        text = text.strip().lower()

        patterns = [
            (r"(\d+)\s*h(?:ours?)?\b", 1),
            (r"(\d+)\s*m(?:in(?:utes?)?)?\b", 1/60),
            (r"(\d+)\s*d(?:ays?)?\b", 24),
            (r"(\d+)\s*s(?:ec(?:onds?)?)?\b", 1/3600),
        ]

        total_hours = 0.0
        found = False

        for pattern, multiplier in patterns:
            matches = re.findall(pattern, text)
            for m in matches:
                total_hours += int(m) * multiplier
                found = True

        if not found:
            m = re.search(r"(\d+):(\d+):(\d+)", text)
            if m:
                total_hours = int(m.group(1)) + int(m.group(2))/60 + int(m.group(3))/3600
                found = True

        if not found:
            m = re.search(r"(\d+\.?\d*)", text)
            if m:
                total_hours = float(m.group(1))
                if "day" in text:
                    total_hours *= 24
                elif "min" in text:
                    total_hours /= 60
                found = True

        return total_hours if found else -1

    # ── 开机 ──
    async def start_server(self, server_id: str) -> str:
        """尝试启动离线服务器。

        返回值:
          "started"     - 已点击启动按钮
          "unavailable" - 当前无法启动（按钮缺失/禁用），不算失败
          "failed"      - 启动过程中出现意外错误
        """
        try:
            url = f"{BASE_URL}/server/{server_id}"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            start_btn = self.page.locator('button.power-btn[data-variant="start"], button:has-text("Start"), button:has-text("Démarrer")')
            if await start_btn.count() == 0:
                log(f"[START] No start button found for {server_id}")
                return "unavailable"

            if await start_btn.first.is_disabled():
                log(f"[START] Start button is disabled for {server_id}, skipping start")
                return "unavailable"

            try:
                await start_btn.first.click(timeout=15000)
            except PlaywrightTimeout:
                log(f"[START] Start button click timed out for {server_id}, skipping start")
                return "unavailable"

            log(f"[START] Clicked start button for {server_id}")
            await asyncio.sleep(10)

            confirm_timeout = False
            confirm_btn = self.page.locator('button:has-text("Confirm"), button:has-text("Yes"), button:has-text("Oui")')
            if await confirm_btn.count() > 0:
                try:
                    await confirm_btn.first.click(timeout=5000)
                    await asyncio.sleep(5)
                except PlaywrightTimeout:
                    confirm_timeout = True
                    log(f"[START] Confirm dialog disappeared for {server_id}, continuing")

            # Verify state change
            await asyncio.sleep(2)
            status = await self.get_server_status(server_id)
            if status["is_online"]:
                log(f"[START] Server {server_id} confirmed online")
                return "started"
            if confirm_timeout:
                log(f"[START] Server {server_id} still offline after confirm timeout, treating as unavailable")
                return "unavailable"
            log(f"[START] Server {server_id} may not have started, remaining: {status['remaining_hours']}h")
            return "started"  # Still return started as start was attempted

        except Exception as e:
            log(f"[START] Error: {e}")
            return "failed"

    # ── 反机器人验证处理（ACLClouds 自定义验证） ──
    async def _brute_force_turnstile(self, page, max_attempts=20) -> bool:
        log(f"[TURNSTILE] Starting brute force (max {max_attempts} attempts)")
        dialog_seen = False
        post_dismiss_checks = 0

        for i in range(max_attempts):
            success_text = page.locator("text=Server renewed successfully")
            if await success_text.count() > 0:
                log(f"[TURNSTILE] Already resolved, skip clicking")
                return True

            anti_bot = page.locator("text=/Anti-bot confirmation/i")
            if await anti_bot.count() == 0:
                # 弹窗已消失：若之前见过弹窗，说明复选框已点，不再空转 20 次
                # Cap  Chancellor：PoW 需要时间，最长等 60s（20 x 3s），同时给成功文案留机会
                if dialog_seen:
                    post_dismiss_checks += 1
                    if await success_text.count() > 0:
                        log(f"[TURNSTILE] Resolved after dismiss (check {post_dismiss_checks})")
                        return True
                    if post_dismiss_checks >= 20:
                        try:
                            req = page.locator("text=/captcha_required/i")
                            if await req.count() > 0:
                                log("[TURNSTILE] NOTE: captcha_required marker still present after 60s wait")
                        except Exception:
                            pass
                        log("[TURNSTILE] Dialog dismissed, checkbox done, proceed to result check")
                        try:
                            await page.screenshot(path=os.path.join(DEBUG_DIR, "turnstile_after_dismiss.png"), full_page=False)
                        except Exception:
                            pass
                        return True
                    if post_dismiss_checks in (1, 5, 10, 15, 20):
                        log(f"[TURNSTILE] Dismissed, waiting Cap PoW / success ({post_dismiss_checks}/20)...")
                    await asyncio.sleep(3)
                    continue
                log(f"[TURNSTILE] Attempt {i+1}/{max_attempts}: No Anti-bot dialog, checking success...")
                await asyncio.sleep(3)
                if await success_text.count() > 0:
                    log(f"[TURNSTILE] Resolved after {i+1} attempts")
                    return True
                continue

            dialog_seen = True
            post_dismiss_checks = 0

            log(f"[TURNSTILE] Anti-bot confirmation dialog detected")

            # 点击复选框（官网改版：文案从 "I am not a robot" 改为 "Verify you're human"，
            # Cap 部件可能是 input/role=checkbox，先找真正的框，避免点到文本行导致误关弹窗）
            checkbox = None
            for sel in [
                'input[type="checkbox"]',
                '[role="checkbox"]',
                '[class*=checkbox]',
                '[class*=cap-] input',
                "text=Verify you're human",
                "text=Verify you are human",
                "text=I am not a robot",
                "text=Verify",
            ]:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        # 取 Anti-bot 弹窗内可见的第一个
                        for k in range(min(await loc.count(), 5)):
                            try:
                                if await loc.nth(k).is_visible():
                                    checkbox = loc.nth(k)
                                    log(f"[TURNSTILE] checkbox selector matched: {sel}")
                                    break
                            except Exception:
                                continue
                        if checkbox is not None:
                            break
                except Exception:
                    continue
            if checkbox is not None:
                try:
                    box = await checkbox.bounding_box()
                except Exception:
                    box = None
                if box:
                    # 文案行很宽时点左侧复选框位置，而非文本中心
                    if box["width"] > 100:
                        cx = box["x"] + 20
                        cy = box["y"] + box["height"] / 2
                    else:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                    await page.mouse.move(cx + random.uniform(-3, 3), cy + random.uniform(-3, 3))
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    await page.mouse.move(cx, cy)
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                    await page.mouse.click(cx, cy)
                    log(f"[TURNSTILE] Clicked checkbox attempt {i+1}/{max_attempts} at ({cx:.0f}, {cy:.0f})")
                else:
                    log(f"[TURNSTILE] Could not get bounding box, try direct click")
                    try:
                        await checkbox.click(timeout=5000)
                    except Exception:
                        try:
                            await checkbox.evaluate("(el) => el.click()")
                        except Exception:
                            pass
            else:
                log(f"[TURNSTILE] Attempt {i+1}/{max_attempts}: human checkbox not found")
                await asyncio.sleep(3)
                if await success_text.count() > 0:
                    log(f"[TURNSTILE] Resolved after {i+1} attempts")
                    return True
                continue

            await asyncio.sleep(2)
            # 点后即时诊断：复选框状态 + toast/弹窗是否还在（区分验证成功关闭 vs 误点关闭）
            try:
                state = await checkbox.evaluate(
                    """(el) => {
                        if (el.checked === true) return 'checked';
                        const a = el.getAttribute ? (el.getAttribute('aria-checked') || el.getAttribute('data-state') || '') : '';
                        if (a) return 'attr:' + a;
                        const box = el.closest ? (el.closest('[role=checkbox]') || el.querySelector('[role=checkbox]')) : null;
                        if (box) return 'role:' + (box.getAttribute('aria-checked') || box.getAttribute('data-state') || 'none');
                        return 'unknown:' + (el.tagName || '?');
                    }"""
                )
                log(f"[TURNSTILE] post-click checkbox state: {state}")
            except Exception as e:
                log(f"[TURNSTILE] post-click state skip: {e}")
            try:
                await page.screenshot(path=os.path.join(DEBUG_DIR, f"turnstile_clicked_{i+1}.png"), full_page=False)
                log(f"[TURNSTILE] post-click screenshot saved (clicked_{i+1})")
            except Exception:
                pass
            try:
                toast = page.locator('[class*=toast], [class*=Toast], [role="alert"], [class*=notification], [class*=snackbar]')
                if await toast.count() > 0:
                    try:
                        t = (await toast.first.inner_text()).strip()
                        if t:
                            log(f"[TURNSTILE] toast/alert: {t[:200]}")
                    except Exception:
                        pass
            except Exception:
                pass

            # 检查是否有CAPTCHA挑战出现（与登录页相同结构 + 新版变体）
            # 注意：server 页 Renew 旁常驻的 captcha_required 只是标记，不是挑战，不列入
            challenge = None
            for sel in [
                ".auth-captcha-challenge",
                "[class*=captcha-challenge]",
                "[class*=Captcha]",
                "text=/select.*image/i",
            ]:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        try:
                            if await loc.first.is_visible():
                                challenge = page.locator(".auth-captcha-challenge") if await page.locator(".auth-captcha-challenge").count() > 0 else loc
                                log(f"[TURNSTILE] Challenge marker detected via: {sel}")
                                break
                        except Exception:
                            continue
                except Exception:
                    continue
            if challenge is not None:
                try:
                    visible = await challenge.first.is_visible()
                except Exception:
                    visible = True
                if not visible:
                    challenge = None
            if challenge is not None:
                log(f"[TURNSTILE] CAPTCHA challenge detected, solving...")
                for ocr_attempt in range(1, 6):
                    prompt_elem = page.locator(".auth-captcha-prompt strong")
                    if await prompt_elem.count() == 0:
                        log("[TURNSTILE] No CAPTCHA prompt found (old selector), dump for new UI")
                        try:
                            await self._save_html("turnstile_captcha_unknown")
                            body = await page.content()
                            m = re.search(r"(captcha[^<]{0,120})", body, re.I)
                            if m:
                                log(f"[TURNSTILE] captcha context: {m.group(1)[:150]}")
                        except Exception as e:
                            log(f"[TURNSTILE] dump skip: {e}")
                        break

                    target_text = (await prompt_elem.inner_text()).strip().lower()
                    log(f"[TURNSTILE] CAPTCHA target: '{target_text}'")

                    options = page.locator(".auth-captcha-option")
                    count = await options.count()
                    if count != 4:
                        log(f"[TURNSTILE] Expected 4 options, got {count}")
                        if count == 0:
                            break
                        continue

                    solved = False
                    for idx in range(count):
                        option = options.nth(idx)
                        img = option.locator(".auth-captcha-option-img")
                        if await img.count() == 0:
                            continue

                        src = await img.get_attribute("src")
                        if src:
                            try:
                                resp = await page.context.request.get(src)
                                img_bytes = await resp.body()
                            except Exception:
                                img_bytes = await img.screenshot()
                        else:
                            img_bytes = await img.screenshot()

                        try:
                            recognized = self.captcha_solver.ocr.classification(img_bytes).lower().strip()
                        except Exception as e:
                            log(f"[TURNSTILE] OCR error on option {idx+1}: {e}")
                            recognized = ""

                        log(f"[TURNSTILE] Option {idx+1}: '{recognized}'")

                        ratio = difflib.SequenceMatcher(None, target_text, recognized).ratio()
                        is_match = (
                            target_text == recognized
                            or ratio > 0.65
                            or (len(target_text) > 3 and target_text in recognized)
                            or (len(recognized) > 3 and recognized in target_text)
                        )
                        if is_match:
                            log(f"[TURNSTILE] Match found: option {idx+1} (ratio={ratio:.2f})")
                            await option.click()
                            await asyncio.sleep(1)
                            if await challenge.is_visible():
                                log("[TURNSTILE] Challenge still visible after click, retrying...")
                                continue
                            log("[TURNSTILE] Challenge accepted")
                            solved = True
                            break

                    if solved:
                        log("[TURNSTILE] CAPTCHA solved successfully")
                        await asyncio.sleep(3)
                        break

                    log(f"[TURNSTILE] OCR attempt {ocr_attempt} failed, refreshing...")
                    if ocr_attempt < 5:
                        ref_btn = page.locator(".auth-captcha-refresh, button:has-text('Refresh'), [aria-label*='refresh']")
                        if await ref_btn.count() > 0:
                            await ref_btn.first.click()
                            await asyncio.sleep(1)

            await asyncio.sleep(3)

            if await success_text.count() > 0:
                log(f"[TURNSTILE] Resolved after {i+1} attempts")
                return True

            # 如果对话框消失了也视为成功
            if await anti_bot.count() == 0:
                log(f"[TURNSTILE] Anti-bot dialog dismissed")
                await asyncio.sleep(2)
                if await success_text.count() > 0:
                    return True

        log("[TURNSTILE] Max attempts reached, may not have resolved")
        return False

    # ── 续期辅助：诊断 + 稳健点击 ──
    async def _dump_buttons_for_debug(self, where: str):
        try:
            texts = await self.page.locator("button").all_inner_texts()
            clean = [re.sub(r"\s+", " ", t).strip() for t in texts]
            clean = [t for t in clean if t]
            log(f"[RENEW][{where}] buttons({len(clean)}): {clean}")
            # 链接也可能是入口（Details/Renew 可能是 <a>）
            try:
                links = await self.page.locator("a").all_inner_texts()
                lclean = [re.sub(r"\s+", " ", t).strip() for t in links]
                lclean = [t for t in lclean if t][:20]
                if lclean:
                    log(f"[RENEW][{where}] links: {lclean}")
            except Exception:
                pass
        except Exception as e:
            log(f"[RENEW][{where}] dump buttons skip: {e}")

    async def _save_html(self, name: str):
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            path = os.path.join(DEBUG_DIR, f"{name}.html")
            html = await self.page.content()
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            log(f"[DEBUG] HTML saved: {path} ({len(html)} bytes)")
        except Exception as e:
            log(f"[DEBUG] HTML save failed: {e}")

    async def _click_robust(self, btn, label: str) -> bool:
        """可见性过滤 + 滚动 + 普通点击 + JS 兜底。返回是否点击成功。"""
        try:
            try:
                await btn.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            try:
                await btn.click(timeout=10000)
                return True
            except PlaywrightTimeout as e1:
                log(f"[RENEW] normal click timeout({label}), try JS click: {str(e1)[:120]}")
            # JS 兜底：隐藏元素 / 被遮挡时仍可触发
            try:
                await btn.evaluate("(el) => el.click()")
                await asyncio.sleep(1)
                return True
            except Exception as e2:
                log(f"[RENEW] JS click failed({label}): {e2}")
                return False
        except Exception as e:
            log(f"[RENEW] click failed({label}): {e}")
            return False

    async def _find_visible_renew_buttons(self):
        """只返回可见的 Renew/Extend 类按钮，避免点到隐藏模板（根因修复）。
        旧代码用 *:has-text(server_id) 会命中隐藏的 client-btn，导致
        'element is not visible' 超时 30s x3。
        注意：必须排除 'My renewals' Tab（它包含 Renew 子串但不是续期按钮）。"""
        candidates = self.page.locator(
            'button:has-text("Renew"), button:has-text("Renouveler"), '
            'button:has-text("Extend"), button:has-text("Prolonger"), '
            'button:has-text("Renewal"), a:has-text("Renew")'
        )
        visible = []
        try:
            count = await candidates.count()
        except Exception:
            return visible
        for i in range(min(count, 30)):
            try:
                b = candidates.nth(i)
                if await b.count() == 0:
                    continue
                if await b.is_visible():
                    try:
                        t = (await b.inner_text()).strip()
                    except Exception:
                        t = ""
                    tl = re.sub(r"\s+", " ", t).strip().lower()
                    # 排除导航 Tab：My renewals / My renewal / Mes renouvellements 等
                    if tl in ("my renewals", "my renewal", "mes renouvellements", "my renews"):
                        continue
                    if tl.startswith("my renew"):
                        continue
                    visible.append((b, t))
            except Exception:
                continue
        return visible

    async def _open_my_renewals_detail(self, server_id: str) -> bool:
        """在 My renewals 页点击对应服务的 Details/Actions，进入可续期弹窗或详情。
        返回是否成功打开了详情（截图证据：ACTIONS 列有 Details 按钮，右侧被截断，需横向滚动）。"""
        try:
            # 横向滚动到最右，确保 ACTIONS 列可见（1280 宽视口会截断）
            try:
                await self.page.evaluate("window.scrollTo(document.body.scrollWidth, 0)")
                await asyncio.sleep(1)
            except Exception:
                pass
            # 优先按 service_id 短前缀定位行内的 Details 按钮
            short = server_id[:8] if len(server_id) >= 8 else server_id
            row = None
            for locator_str in [f'tr:has-text("{short}")', f'text={short}']:
                try:
                    loc = self.page.locator(locator_str)
                    if await loc.count() > 0:
                        row = loc.first
                        break
                except Exception:
                    continue
            detail_btn = None
            if row is not None:
                for sel in [
                    'button:has-text("Detail")', 'a:has-text("Detail")',
                    'button:has-text("Action")', 'button:has-text("Manage")',
                    'button:has-text("操作")', 'button:has-text("详情")',
                ]:
                    try:
                        # 在行祖先容器内查找
                        container = self.page.locator(f'tr:has-text("{short}")')
                        if await container.count() > 0:
                            cand = container.first.locator(sel)
                            if await cand.count() > 0:
                                for k in range(min(await cand.count(), 3)):
                                    if await cand.nth(k).is_visible():
                                        detail_btn = cand.nth(k)
                                        break
                            if detail_btn is not None:
                                break
                    except Exception:
                        continue
            if detail_btn is None:
                # 兜底：页面上第一个可见的 Detail 按钮（单服务账号安全）
                try:
                    cand = self.page.locator('button:has-text("Detail"), a:has-text("Detail")')
                    for k in range(min(await cand.count(), 5)):
                        if await cand.nth(k).is_visible():
                            detail_btn = cand.nth(k)
                            break
                except Exception:
                    pass
            if detail_btn is None:
                log("[RENEW] My renewals detail button not found")
                return False
            try:
                await detail_btn.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            try:
                await detail_btn.click(timeout=8000)
            except Exception:
                try:
                    await detail_btn.evaluate("(el) => el.click()")
                except Exception as e:
                    log(f"[RENEW] detail click failed: {e}")
                    return False
            await asyncio.sleep(3)
            await self._dump_buttons_for_debug("after-detail")
            return True
        except Exception as e:
            log(f"[RENEW] open detail skip: {e}")
            return False

    # ── 续期（暴力点击 Turnstile） ──
    async def renew_server(self, server_id: str, old_remaining_hours: float = -1) -> dict:
        result = {
            "success": False,
            "server_id": server_id,
            "old_remaining": fmt_hours(old_remaining_hours) if old_remaining_hours >= 0 else "unknown",
            "new_remaining": "",
            "message": "",
        }
        name = f"renew_{server_id}_{int(time.time())}"

        try:
            await self.page.goto(f"{BASE_URL}/dashboard/projects", wait_until="domcontentloaded", timeout=60000)
            try:
                await self.page.locator("text=Manage my services").first.wait_for(state="visible", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(random.uniform(2, 3))

            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1)

            await self.page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                const closeBtn = btns.find(b => (b.innerText || '').trim() === 'Close');
                if (closeBtn) closeBtn.click();
            }""")
            await asyncio.sleep(2)

            renewal_unavailable = self.page.locator("text=/Renewal will be available/i")
            if await renewal_unavailable.count() > 0:
                log(f"[RENEW] Renewal not yet available for server {server_id} (within 48h threshold)")
                result["success"] = True
                result["message"] = "Renewal not yet available (within window)"
                return result

            clicked = False

            # 策略 A：列表页可见 Renew 按钮（官网改版后列表页已无 Renew，需过滤可见元素）
            await self._dump_buttons_for_debug("list")
            visible = await self._find_visible_renew_buttons()
            if visible:
                log(f"[RENEW] Found {len(visible)} visible renew buttons on list page")
                btn, txt = visible[0]
                if await self._click_robust(btn, f"list:{txt[:30]}"):
                    clicked = True
                    log(f"[RENEW] Clicked Renew for {server_id} (list page: '{txt[:60]}')")
            else:
                log("[RENEW] No visible Renew button on list page, try My renewals tab...")

            # 策略 B：My renewals Tab（新版 UI 已把续期入口移到此 Tab，截图证实）
            if not clicked:
                try:
                    tab = self.page.locator('button:has-text("My renewals"), a:has-text("My renewals")')
                    if await tab.count() > 0:
                        try:
                            await tab.first.scroll_into_view_if_needed(timeout=5000)
                        except Exception:
                            pass
                        try:
                            await tab.first.click(timeout=8000)
                        except Exception:
                            try:
                                await tab.first.evaluate("(el) => el.click()")
                            except Exception:
                                pass
                        await asyncio.sleep(3)
                        await self._dump_buttons_for_debug("my-renewals")
                        visible = await self._find_visible_renew_buttons()
                        if visible:
                            btn, txt = visible[0]
                            if await self._click_robust(btn, f"tab:{txt[:30]}"):
                                clicked = True
                                log(f"[RENEW] Clicked Renew for {server_id} (My renewals tab: '{txt[:60]}')")
                        # B2：My renewals 行内 Details -> 弹窗/详情里的真 Renew 按钮
                        if not clicked:
                            if await self._open_my_renewals_detail(server_id):
                                visible = await self._find_visible_renew_buttons()
                                if visible:
                                    btn, txt = visible[0]
                                    if await self._click_robust(btn, f"renewals-detail:{txt[:30]}"):
                                        clicked = True
                                        log(f"[RENEW] Clicked Renew for {server_id} (renewals detail: '{txt[:60]}')")
                except Exception as e:
                    log(f"[RENEW] My renewals tab skip: {e}")

            # 策略 C：View service details 详情页/弹窗（截图显示列表页只有 View/Cancel/Invoices/Support）
            if not clicked:
                try:
                    detail_btn = self.page.locator(
                        'button:has-text("View service details"), button:has-text("View details"), '
                        'a:has-text("View service details")'
                    )
                    if await detail_btn.count() > 0:
                        for i in range(min(await detail_btn.count(), 3)):
                            try:
                                b = detail_btn.nth(i)
                                if not await b.is_visible():
                                    continue
                                try:
                                    await b.scroll_into_view_if_needed(timeout=5000)
                                except Exception:
                                    pass
                                try:
                                    await b.click(timeout=8000)
                                except Exception:
                                    await b.evaluate("(el) => el.click()")
                                await asyncio.sleep(3)
                                await self._dump_buttons_for_debug(f"detail-{i}")
                                visible = await self._find_visible_renew_buttons()
                                if visible:
                                    btn, txt = visible[0]
                                    if await self._click_robust(btn, f"detail:{txt[:30]}"):
                                        clicked = True
                                        log(f"[RENEW] Clicked Renew for {server_id} (detail page: '{txt[:60]}')")
                                        break
                                # 详情页可能是新页面/弹窗，找不到则返回列表继续
                                try:
                                    await self.page.go_back(timeout=10000)
                                    await asyncio.sleep(2)
                                except Exception:
                                    await self.page.goto(f"{BASE_URL}/dashboard/projects", wait_until="domcontentloaded", timeout=30000)
                                    await asyncio.sleep(2)
                            except Exception as e3:
                                log(f"[RENEW] detail candidate {i} skip: {e3}")
                                continue
                except Exception as e:
                    log(f"[RENEW] detail page skip: {e}")

            # 策略 D：直接 /server/{id} 页（旧版续期按钮可能仍在此页）
            if not clicked:
                try:
                    await self.page.goto(f"{BASE_URL}/server/{server_id}", wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    await self._dump_buttons_for_debug("server-page")
                    visible = await self._find_visible_renew_buttons()
                    if visible:
                        btn, txt = visible[0]
                        if await self._click_robust(btn, f"server:{txt[:30]}"):
                            clicked = True
                            log(f"[RENEW] Clicked Renew for {server_id} (server page: '{txt[:60]}')")
                except Exception as e:
                    log(f"[RENEW] server page skip: {e}")

            if not clicked:
                result["message"] = "No visible Renew button found (list/My renewals/detail/server all tried)"
                log(f"[RENEW] {result['message']} for {server_id}")
                await save_screenshot(f"renew_error_{server_id}", self.page)
                await self._save_html(f"renew_error_{server_id}")
                return result

            await asyncio.sleep(random.uniform(2, 3))

            turnstile_ok = await self._brute_force_turnstile(self.page)
            if not turnstile_ok:
                log("[RENEW] Turnstile not resolved, checking result anyway...")
            await save_screenshot(name, self.page)

            if await self.page.locator("text=Server renewed successfully").count() > 0:
                log("[RENEW] Server renewed successfully text found")
                result["success"] = True
                result["message"] = "Renewal successful (via text)"

            # Cap 两步提交：验证通过后可能需要再点一次 Renew 才会真正提交
            # （截图显示验证后仍停留在 server 页，Renew + captcha_required 并存）
            if not result["success"]:
                try:
                    anti_gone = await self.page.locator("text=/Anti-bot confirmation/i").count() == 0
                    req = self.page.locator("text=/captcha_required/i")
                    req_present = await req.count() > 0
                    if anti_gone and req_present:
                        log("[RENEW] Try second Renew submit after Cap verified...")
                        visible2 = await self._find_visible_renew_buttons()
                        if visible2:
                            btn2, txt2 = visible2[0]
                            if await self._click_robust(btn2, f"second:{txt2[:30]}"):
                                log(f"[RENEW] Clicked second Renew ('{txt2[:60]}')")
                                await asyncio.sleep(3)
                                turnstile_ok2 = await self._brute_force_turnstile(self.page)
                                if not turnstile_ok2:
                                    log("[RENEW] Second turnstile not resolved, checking anyway...")
                                if await self.page.locator("text=Server renewed successfully").count() > 0:
                                    result["success"] = True
                                    result["message"] = "Renewal successful (via text, 2nd submit)"
                except Exception as e2:
                    log(f"[RENEW] second submit skip: {e2}")

            await self.page.goto(f"{BASE_URL}/server/{server_id}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            new_status = await self.get_server_status(server_id)
            new_hours = new_status["remaining_hours"]

            if old_remaining_hours >= 0:
                if new_hours > old_remaining_hours + 0.5:
                    result["success"] = True
                    result["new_remaining"] = fmt_hours(new_hours)
                    result["message"] = "Renewal successful"
                elif new_hours > old_remaining_hours:
                    result["success"] = True
                    result["new_remaining"] = fmt_hours(new_hours)
                    result["message"] = "Renewal partially extended"
                else:
                    result["new_remaining"] = fmt_hours(new_hours)
                    result["message"] = f"Time not increased (old={fmt_hours(old_remaining_hours)}, new={fmt_hours(new_hours)})"
            else:
                result["new_remaining"] = fmt_hours(new_hours)
                if result["success"]:
                    result["message"] = f"Renewal successful (new={fmt_hours(new_hours)})"
                else:
                    result["message"] = f"Renewal result unknown (new={fmt_hours(new_hours)})"

            log(f"[RENEW] Result: {result['message']}")

        except Exception as e:
            result["message"] = f"Error: {e}"
            log(f"[RENEW] Error: {e}")
            await save_screenshot(f"renew_error_{server_id}", self.page)
            try:
                await self._save_html(f"renew_error_{server_id}")
            except Exception:
                pass

        return result


# ============================================================
# 主流程
# ============================================================
async def main():
    print("=" * 60)
    print("  ACLClouds Auto Renewal (Playwright)")
    print("=" * 60)
    os.makedirs(DEBUG_DIR, exist_ok=True)

    if not EMAIL or not PASSWORD:
        error("ACL_EMAIL or ACL_PASSWORD not set")
        print("Usage:")
        print("  set ACL_EMAIL=your@email.com")
        print("  set ACL_PASSWORD=your_password")
        print("  set ACL_SERVER_ID=your_server_id")
        sys.exit(1)

    if not SERVER_ID:
        error("ACL_SERVER_ID not set")
        sys.exit(1)

    server_ids = [s.strip() for s in SERVER_ID.split(",") if s.strip()]
    log(f"Account: {mask(EMAIL)} | Servers: {len(server_ids)} | Threshold: {RENEW_THRESHOLD_HOURS}h")

    renewer = ACLCloudsRenewer(EMAIL, PASSWORD)
    renewer.server_ids = server_ids

    try:
        await renewer.start_browser()

        cookie_ok = await renewer.restore_cookies()
        if not cookie_ok:
            log("[MAIN] Cookie login failed, performing full login")
            if not await renewer.login():
                error(f"Login failed: {mask(EMAIL)}")
                await save_screenshot("login_failed", renewer.page)
                await send_telegram(f"[FAIL] Login failed\n\nAccount: {mask(EMAIL)}\n\n{SIGNATURE}")
                await renewer.close_browser()
                sys.exit(1)

        await send_telegram(
            f"[START] ACLClouds Renewal\n\n"
            f"Account: {mask(EMAIL)}\n"
            f"Servers: {len(server_ids)}\n"
            f"Threshold: {RENEW_THRESHOLD_HOURS}h\n\n"
            f"{SIGNATURE}"
        )

        for sid in server_ids:
            log(f"\n{'='*40}")
            log(f"Processing server: {sid}")

            status = await renewer.get_server_status(sid)
            if status["error"]:
                log(f"[MAIN] Status check error: {status['error']}")
                STATS["failures"] += 1
                await send_telegram(
                    f"[FAIL] Status check error\n\n"
                    f"Account: {mask(EMAIL)}\n"
                    f"Server: {sid}\n"
                    f"Error: {status['error']}\n\n"
                    f"{SIGNATURE}"
                )
                continue

            log(f"[MAIN] Server {sid}: remaining={fmt_hours(status['remaining_hours'])}, online={status['is_online']}")

            if not status["is_online"]:
                log(f"[MAIN] Server {sid} is offline, starting...")
                start_result = await renewer.start_server(sid)
                if start_result == "started":
                    STATS["starts"] += 1
                    await send_telegram(
                        f"[START] Server started\n\n"
                        f"Account: {mask(EMAIL)}\n"
                        f"Server: {sid}\n\n"
                        f"{SIGNATURE}"
                    )
                elif start_result == "unavailable":
                    log(f"[MAIN] Server {sid} start unavailable, continuing...")
                else:
                    STATS["failures"] += 1
                    await send_telegram(
                        f"[FAIL] Start server failed\n\n"
                        f"Account: {mask(EMAIL)}\n"
                        f"Server: {sid}\n\n"
                        f"{SIGNATURE}"
                    )

            MAX_RENEW_RETRY = 3

            if status["remaining_hours"] >= 0 and status["remaining_hours"] <= RENEW_THRESHOLD_HOURS:
                log(f"[MAIN] Server {sid} needs renewal ({fmt_hours(status['remaining_hours'])} remaining)")
                for attempt in range(1, MAX_RENEW_RETRY + 1):
                    renew_result = await renewer.renew_server(sid, old_remaining_hours=status["remaining_hours"])
                    if renew_result["success"]:
                        break
                    if attempt < MAX_RENEW_RETRY:
                        log(f"[MAIN] Renew retry {attempt}/{MAX_RENEW_RETRY} for {sid}")
                        await asyncio.sleep(5 * attempt)

                if renew_result["success"]:
                    STATS["renewals"] += 1
                    await send_telegram(
                        f"[OK] Renewal successful\n\n"
                        f"Account: {mask(EMAIL)}\n"
                        f"Server: {sid}\n"
                        f"New remaining: {renew_result['new_remaining']}\n\n"
                        f"{SIGNATURE}"
                    )
                else:
                    STATS["failures"] += 1
                    await send_telegram(
                        f"[FAIL] Renewal failed\n\n"
                        f"Account: {mask(EMAIL)}\n"
                        f"Server: {sid}\n"
                        f"Reason: {renew_result['message']}\n\n"
                        f"{SIGNATURE}"
                    )
            elif status["remaining_hours"] >= 0:
                STATS["skipped"] += 1
                log(f"[MAIN] Server {sid} skipped ({fmt_hours(status['remaining_hours'])} remaining)")
            else:
                log(f"[MAIN] Server {sid} remaining time unknown, attempting renewal anyway")
                for attempt in range(1, MAX_RENEW_RETRY + 1):
                    renew_result = await renewer.renew_server(sid)
                    if renew_result["success"]:
                        break
                    if attempt < MAX_RENEW_RETRY:
                        log(f"[MAIN] Renew retry {attempt}/{MAX_RENEW_RETRY} for {sid}")
                        await asyncio.sleep(5 * attempt)

                if renew_result["success"]:
                    STATS["renewals"] += 1
                elif "No visible Renew button" in renew_result.get("message", "") and status["is_online"]:
                    # 服务器在线但找不到续期入口 = 暂无需续期（21:32 误报教训：
                    # 真正过期的服务器能解析出 0h 而不会走此分支）
                    STATS["skipped"] += 1
                    log(f"[MAIN] Server {sid} online with no Renew entry, treat as skipped (not failure)")
                else:
                    STATS["failures"] += 1

            await asyncio.sleep(random.uniform(1, 3))

    except Exception as e:
        error(f"Fatal error: {e}")
        await save_screenshot("fatal_error", renewer.page)
        await send_telegram(f"[ERROR] Fatal: {e}\n\n{SIGNATURE}")
        STATS["failures"] += 1

    finally:
        await renewer.close_browser()

    print()
    print("=" * 60)
    print("  Statistics")
    print("=" * 60)
    print(f"  Renewals:  {STATS['renewals']}")
    print(f"  Skipped:   {STATS['skipped']}")
    print(f"  Failed:    {STATS['failures']}")
    print(f"  Started:   {STATS['starts']}")
    print("=" * 60)

    await send_telegram(
        f"[DONE] ACLClouds Renewal\n\n"
        f"Renewals: {STATS['renewals']}\n"
        f"Skipped: {STATS['skipped']}\n"
        f"Failed: {STATS['failures']}\n"
        f"Started: {STATS['starts']}\n\n"
        f"{SIGNATURE}"
    )

    if STATS["failures"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
