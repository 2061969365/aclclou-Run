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
import os
import sys
import time
import pickle
import random
import asyncio
import tempfile
from datetime import datetime

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
    return os.path.join(COOKIE_DIR, f"{email_hash}.pkl")


def save_cookies(email: str, cookies: list):
    try:
        path = get_cookie_path(email)
        data = {"cookies": cookies, "email": email, "saved_at": time.time()}
        with open(path, "wb") as f:
            pickle.dump(data, f)
        log(f"[COOKIE] Saved: {path}")
    except Exception as e:
        log(f"[COOKIE] Save failed: {e}")


def load_cookies(email: str) -> list | None:
    try:
        path = get_cookie_path(email)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            data = pickle.load(f)
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
            log("[CAPTCHA] No checkbox found")
            return False

        await checkbox.click()
        log("[CAPTCHA] Clicked checkbox")
        await asyncio.sleep(2)

        challenge = page.locator(".auth-captcha-challenge")
        try:
            await challenge.wait_for(state="visible", timeout=10000)
        except PlaywrightTimeout:
            log("[CAPTCHA] Challenge not visible after click")
            return False

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

                    if target_text == recognized or target_text in recognized.split() or recognized in target_text.split():
                        log(f"[CAPTCHA] Match found: option {idx+1}")
                        await option.click()
                        await asyncio.sleep(1)
                        return True

                log("[CAPTCHA] No match, refreshing images...")
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
            viewport={"width": 1280, "height": 720},
        )
        self.page = await self.context.new_page()
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
            await self.page.goto(f"{BASE_URL}/server/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            if "/server/" in self.page.url and "/auth/" not in self.page.url:
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
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await asyncio.sleep(random.uniform(1, 2))

                if "challenge" in self.page.url.lower() or "cloudflare" in (await self.page.content()).lower():
                    wait = 10 + attempt * 3
                    log(f"[LOGIN] Cloudflare detected, waiting {wait}s...")
                    await asyncio.sleep(wait)
                    continue

                email_input = self.page.locator('input[name="email"], input[type="email"], #username')
                if await email_input.count() == 0:
                    log("[LOGIN] Email input not found")
                    await asyncio.sleep(3)
                    continue

                await email_input.press_sequentially(self.email, delay=random.randint(50, 120))
                await asyncio.sleep(random.uniform(0.3, 0.6))

                password_input = self.page.locator('input[name="password"], input[type="password"], #password')
                if await password_input.count() == 0:
                    log("[LOGIN] Password input not found")
                    continue

                await password_input.press_sequentially(self.password, delay=random.randint(50, 120))
                await asyncio.sleep(random.uniform(0.3, 0.6))

                captcha_ok = await self.captcha_solver.solve(self.page, max_retries=5)
                if not captcha_ok:
                    log("[LOGIN] Captcha failed")
                    continue

                login_btn = self.page.locator('button:has-text("Sign in"), button:has-text("Login"), button[type="submit"]')
                if await login_btn.count() == 0:
                    log("[LOGIN] Login button not found")
                    continue

                async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                    await login_btn.first.click()
                await asyncio.sleep(2)

                current_url = self.page.url
                page_content = await self.page.content()

                if "/server/" in current_url and "/auth/" not in current_url:
                    log(f"[LOGIN] Success: {mask(self.email)} (attempt {attempt})")
                    self.logged_in = True
                    cookies = await self.context.cookies()
                    save_cookies(self.email, cookies)
                    return True

                if "incorrect" in page_content.lower() or "invalid" in page_content.lower():
                    if "password" in page_content.lower():
                        log(f"[LOGIN] Wrong password: {mask(self.email)}")
                        return False
                    if "captcha" in page_content.lower() or "code" in page_content.lower():
                        log(f"[LOGIN] Captcha error, retrying...")
                        await asyncio.sleep(random.uniform(1, 2))
                        continue

                log(f"[LOGIN] Unknown result, page size: {len(page_content)}")
                await asyncio.sleep(random.uniform(2, 4))

            except Exception as e:
                log(f"[LOGIN] Error: {e}")
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

            content = await self.page.content()

            time_text = ""
            for selector in [
                "text=Time remaining",
                "text=Temps restant",
                ".time-remaining",
                "[class*=time]",
                "[class*=countdown]",
            ]:
                elem = self.page.locator(selector)
                if await elem.count() > 0:
                    time_text = await elem.first.inner_text()
                    break

            if time_text:
                log(f"[STATUS] Raw time text: '{time_text}'")
                hours = self._parse_remaining_hours(time_text)
                result["remaining_hours"] = hours

            power_status = None
            online_indicators = [
                ".status-online",
                "text=Online",
                "text=En ligne",
                "[data-status=online]",
            ]
            for sel in online_indicators:
                elem = self.page.locator(sel)
                if await elem.count() > 0:
                    result["is_online"] = True
                    break

            if not result["is_online"]:
                power_status = await self.page.evaluate("""() => {
                    const knownStates = ['Offline', 'Online', 'Running', 'Starting', 'Stopping', 'Restarting'];
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
                if power_status in ("Online", "Running", "Starting", "Restarting"):
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
        import re
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
    async def start_server(self, server_id: str) -> bool:
        try:
            url = f"{BASE_URL}/server/{server_id}"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            start_btn = self.page.locator('button.power-btn[data-variant="start"], button:has-text("Start"), button:has-text("Démarrer")')
            if await start_btn.count() > 0:
                await start_btn.first.click()
                log(f"[START] Clicked start button for {server_id}")
                await asyncio.sleep(10)

                confirm_btn = self.page.locator('button:has-text("Confirm"), button:has-text("Yes"), button:has-text("Oui")')
                if await confirm_btn.count() > 0:
                    await confirm_btn.first.click()
                    await asyncio.sleep(5)

                log(f"[START] Server {server_id} started")
                return True
            else:
                log(f"[START] No start button found for {server_id}")
                return False

        except Exception as e:
            log(f"[START] Error: {e}")
            return False

    # ── 续期 ──
    async def renew_server(self, server_id: str, old_remaining_hours: float = -1) -> dict:
        result = {
            "success": False,
            "server_id": server_id,
            "old_remaining": "",
            "new_remaining": "",
            "message": "",
        }

        try:
            url = f"{BASE_URL}/server/{server_id}"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            content = await self.page.content()

            renew_btn = self.page.locator(
                'button:has-text("Renew"), button:has-text("Renouveler"), '
                'button:has-text("Extend"), button:has-text("Prolonger"), '
                'a:has-text("Renew"), a:has-text("Renouveler")'
            )

            if await renew_btn.count() == 0:
                result["message"] = "Renew button not found"
                log(f"[RENEW] {result['message']}")
                return result

            await renew_btn.first.click()
            log("[RENEW] Clicked renew button")
            await asyncio.sleep(3)

            confirm_btn = self.page.locator(
                'button:has-text("Confirm"), button:has-text("Yes"), '
                'button:has-text("Oui"), button:has-text("OK")'
            )
            if await confirm_btn.count() > 0:
                await confirm_btn.first.click()
                await asyncio.sleep(2)

            loading_indicators = [
                "text=Renewing...",
                "text=Renouvellement...",
                "text=Processing...",
                ".loading",
                ".spinner",
            ]
            for sel in loading_indicators:
                elem = self.page.locator(sel)
                if await elem.count() > 0:
                    log("[RENEW] Waiting for renewal to complete...")
                    try:
                        await elem.first.wait_for(state="hidden", timeout=60000)
                    except PlaywrightTimeout:
                        pass
                    break

            await asyncio.sleep(3)
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)

            new_status = await self.get_server_status(server_id)
            new_hours = new_status["remaining_hours"]
            if new_hours > 0 and (old_remaining_hours < 0 or new_hours > old_remaining_hours + 0.5):
                result["success"] = True
                result["new_remaining"] = fmt_hours(new_hours)
                result["message"] = "Renewal successful"
            elif old_remaining_hours >= 0 and new_hours > old_remaining_hours:
                result["success"] = True
                result["new_remaining"] = fmt_hours(new_hours)
                result["message"] = "Renewal partially extended"
            else:
                result["message"] = "Renewal result unknown"

            log(f"[RENEW] Result: {result['message']}")

        except Exception as e:
            result["message"] = f"Error: {e}"
            log(f"[RENEW] Error: {e}")

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

    try:
        await renewer.start_browser()

        cookie_ok = await renewer.restore_cookies()
        if not cookie_ok:
            log("[MAIN] Cookie login failed, performing full login")
            if not await renewer.login():
                error(f"Login failed: {mask(EMAIL)}")
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
                start_ok = await renewer.start_server(sid)
                if start_ok:
                    STATS["starts"] += 1
                    await send_telegram(
                        f"[START] Server started\n\n"
                        f"Account: {mask(EMAIL)}\n"
                        f"Server: {sid}\n\n"
                        f"{SIGNATURE}"
                    )
                else:
                    STATS["failures"] += 1
                    await send_telegram(
                        f"[FAIL] Start server failed\n\n"
                        f"Account: {mask(EMAIL)}\n"
                        f"Server: {sid}\n\n"
                        f"{SIGNATURE}"
                    )

            if status["remaining_hours"] >= 0 and status["remaining_hours"] <= RENEW_THRESHOLD_HOURS:
                log(f"[MAIN] Server {sid} needs renewal ({fmt_hours(status['remaining_hours'])} remaining)")
                renew_result = await renewer.renew_server(sid, old_remaining_hours=status["remaining_hours"])

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
                renew_result = await renewer.renew_server(sid)
                if renew_result["success"]:
                    STATS["renewals"] += 1
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
