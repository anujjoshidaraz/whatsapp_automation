"""
WhatsApp Web automation using Playwright (async).
Sends messages to WhatsApp groups/contacts from a persistent browser profile.
Uses Playwright instead of Selenium for better reliability with WhatsApp's React UI.

Connection reliability features:
- Auto-reconnect on disconnect (up to 2 retries per send)
- Periodic health checks to detect stale sessions
- "Phone disconnected" banner detection
- Connection state caching to avoid expensive repeated checks
"""

import asyncio
import os
import re
import subprocess
import threading
import time
from pathlib import Path

# Persistent profile directory
PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".qoderwork", "whatsapp_profile")

# Global browser context
_playwright = None
_context = None
_page = None
_lock = threading.Lock()
_loop = None
_loop_thread = None

# Connection state tracking
_connection_state = {
    "logged_in": False,
    "last_check": 0,
    "last_success": 0,
    "consecutive_failures": 0,
}
_STATE_CACHE_TTL = 30  # seconds before re-checking connection


def _kill_stale_browsers():
    """Kill any stale Playwright Chromium processes that might block launch."""
    try:
        subprocess.run(
            ['powershell', '-Command',
             "Get-Process chromium -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"],
            capture_output=True, timeout=10
        )
    except Exception as e:
        print(f"Warning: Could not kill stale browsers: {e}")

    # Clean up lock files that prevent launch
    for lock_name in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
        lock_path = os.path.join(PROFILE_DIR, lock_name)
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except:
            pass


def _start_event_loop():
    """Run an event loop in a dedicated thread for async Playwright."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _ensure_loop():
    """Ensure the event loop thread is running."""
    global _loop_thread
    if _loop_thread is None or not _loop_thread.is_alive():
        _loop_thread = threading.Thread(target=_start_event_loop, daemon=True)
        _loop_thread.start()
        time.sleep(0.5)


def _run_async(coro):
    """Run an async coroutine in the dedicated loop thread and wait for result."""
    _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=120)


async def _get_browser():
    """Get or create the Playwright browser with persistent context."""
    global _playwright, _context, _page

    if _context is not None:
        try:
            _ = _context.pages
            if _page and not _page.is_closed():
                return _playwright, _context, _page
        except:
            pass
        # Context is dead, clean up
        try:
            await _context.close()
        except:
            pass
        _playwright = None
        _context = None
        _page = None

    _kill_stale_browsers()
    os.makedirs(PROFILE_DIR, exist_ok=True)

    from playwright.async_api import async_playwright

    _playwright = await async_playwright().start()

    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        ignore_https_errors=True,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-sync',
            '--no-first-run',
            '--window-position=-10000,0',
            '--window-size=1200,900',
        ]
    )

    if _context.pages:
        _page = _context.pages[0]
    else:
        _page = await _context.new_page()

    return _playwright, _context, _page


async def _check_disconnected_banner(page):
    """Check if WhatsApp Web shows the 'phone disconnected' banner."""
    try:
        # WhatsApp shows a banner at top when phone is disconnected
        banner_selectors = [
            '[data-testid="alert-phone-disconnected"]',
            '[data-testid="alert-large"]',
            'div[class*="alert"]',
        ]
        for sel in banner_selectors:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if 'disconnect' in text.lower() or 'phone' in text.lower():
                    return True
        return False
    except:
        return False


async def _ensure_on_whatsapp(page):
    """Make sure we're on WhatsApp Web and logged in. Navigate if needed."""
    current_url = page.url or ""
    if "web.whatsapp.com" not in current_url:
        await page.goto('https://web.whatsapp.com', wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(3)

    # Check for logged-in state
    try:
        await page.wait_for_selector('#pane-side', timeout=8000)
        return True
    except:
        return False


async def _async_is_logged_in(force_check=False):
    """Async check if WhatsApp Web is logged in. Caches result for STATE_CACHE_TTL seconds."""
    global _connection_state

    # Use cache if recent enough
    now = time.time()
    if not force_check and (now - _connection_state["last_check"]) < _STATE_CACHE_TTL:
        return _connection_state["logged_in"]

    try:
        _, _, page = await _get_browser()

        # Quick check: are we on WhatsApp Web already?
        current_url = page.url or ""
        if "web.whatsapp.com" not in current_url:
            navigated = await _async_navigate_to_whatsapp(page, max_retries=2)
            if not navigated:
                _connection_state["logged_in"] = False
                _connection_state["last_check"] = time.time()
                return False
            await asyncio.sleep(3)

        # Check for logged-in pane
        try:
            await page.wait_for_selector('#pane-side', timeout=10000)
            _connection_state["logged_in"] = True
            _connection_state["last_check"] = now
            return True
        except:
            # Check if QR code is shown
            try:
                qr = await page.query_selector('[data-icon="qr-code"]')
                if qr:
                    _connection_state["logged_in"] = False
                    _connection_state["last_check"] = now
                    return False
            except:
                pass

            # Check for disconnected banner
            if await _check_disconnected_banner(page):
                print("[WA] Phone disconnected banner detected")
                _connection_state["logged_in"] = False
                _connection_state["last_check"] = now
                return False

            _connection_state["logged_in"] = False
            _connection_state["last_check"] = now
            return False

    except Exception as e:
        print(f"WhatsApp check failed: {e}")
        await _reset_browser()
        _connection_state["logged_in"] = False
        _connection_state["last_check"] = time.time()
        return False


def is_logged_in():
    """Check if WhatsApp Web is logged in (sync wrapper)."""
    print("[WA] is_logged_in called")
    try:
        result = _run_async(_async_is_logged_in(force_check=True))
        print(f"[WA] is_logged_in result: {result}")
        return result
    except Exception as e:
        print(f"[WA] is_logged_in exception: {e}")
        return False


def get_connection_state():
    """Get detailed connection state info (for dashboard API)."""
    return {
        "logged_in": _connection_state["logged_in"],
        "last_check": _connection_state["last_check"],
        "last_success": _connection_state["last_success"],
        "consecutive_failures": _connection_state["consecutive_failures"],
    }


def _is_phone_query(query):
    """Detect if query is a phone number (vs group name)."""
    digits = re.sub(r'\D', '', query)
    if len(digits) >= 8:
        digit_ratio = len(digits) / len(query.replace(' ', ''))
        if digit_ratio >= 0.6:
            return True
    return False


def _normalize_name(name):
    """Normalize group name for comparison."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


async def _async_send_message_to_group(group_name, message, retry_count=0):
    """Async send a message to a WhatsApp group or contact. Auto-retries on failure."""
    try:
        _, _, page = await _get_browser()

        # Ensure we're on WhatsApp Web and logged in
        current_url = page.url or ""
        if "web.whatsapp.com" not in current_url:
            navigated = await _async_navigate_to_whatsapp(page, max_retries=2)
            if not navigated:
                if retry_count < 2:
                    print(f"[WA] Network error, retry {retry_count + 1}/2")
                    await asyncio.sleep(3)
                    return await _async_send_message_to_group(group_name, message, retry_count + 1)
                return {"success": False, "error": "Cannot reach web.whatsapp.com. Check your internet connection."}
            await asyncio.sleep(3)

        # Check if logged in
        try:
            await page.wait_for_selector('#pane-side', timeout=8000)
        except:
            # Check for disconnected banner
            if await _check_disconnected_banner(page):
                print("[WA] Phone disconnected — attempting auto-reconnect")
                await _reset_browser()
                if retry_count < 2:
                    print(f"[WA] Retry {retry_count + 1}/2")
                    await asyncio.sleep(2)
                    return await _async_send_message_to_group(group_name, message, retry_count + 1)
            return {"success": False, "error": "Not logged in to WhatsApp Web. Please run setup first."}

        # Click on search box
        search_box = None
        search_selectors = [
            'input[aria-label="Search or start a new chat"]',
            'input[placeholder="Search or start a new chat"]',
            'input[role="textbox"][data-tab="3"]',
            'input[role="textbox"]',
        ]
        for sel in search_selectors:
            try:
                search_box = page.locator(sel).first
                await search_box.click(timeout=3000)
                break
            except:
                search_box = None
                continue

        if search_box is None:
            # Might be a transient DOM issue — retry
            if retry_count < 2:
                print(f"[WA] Search box not found, retry {retry_count + 1}/2")
                await asyncio.sleep(2)
                return await _async_send_message_to_group(group_name, message, retry_count + 1)
            return {"success": False, "error": "Could not find WhatsApp search box. WhatsApp Web UI may have changed."}

        await asyncio.sleep(0.5)

        # Type the group/contact name
        await search_box.fill(group_name)
        await asyncio.sleep(2)

        is_phone = _is_phone_query(group_name)

        if is_phone:
            try:
                first_result = page.locator('span[title]').first
                await first_result.click(force=True, timeout=5000)
            except:
                return {"success": False, "error": f"Phone number '{group_name}' not found"}
        else:
            normalized_target = _normalize_name(group_name)
            found = False

            try:
                group_item = page.locator(f'span[title="{group_name}"]').first
                await group_item.click(force=True, timeout=5000)
                found = True
            except:
                pass

            if not found:
                try:
                    spans = await page.locator('span[title]').all()
                    for span in spans:
                        title = await span.get_attribute('title')
                        if title and (normalized_target in _normalize_name(title) or
                                     _normalize_name(title) in normalized_target):
                            await span.click(force=True)
                            found = True
                            break
                except:
                    pass

            if not found:
                return {"success": False, "error": f"Group '{group_name}' not found"}

        await asyncio.sleep(1)

        # Find message input box
        message_box = None
        message_selectors = [
            'footer div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][role="textbox"][data-tab="10"]',
            'div[contenteditable="true"][data-tab="10"]',
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"]',
        ]
        for sel in message_selectors:
            try:
                candidate = page.locator(sel).first
                await candidate.wait_for(timeout=5000)
                message_box = candidate
                break
            except:
                continue

        if message_box is None:
            if retry_count < 2:
                print(f"[WA] Message box not found, retry {retry_count + 1}/2")
                await asyncio.sleep(2)
                return await _async_send_message_to_group(group_name, message, retry_count + 1)
            return {"success": False, "error": "Could not find WhatsApp message input box."}

        # Clear and type message
        await message_box.fill('')
        await asyncio.sleep(0.3)

        lines = message.split('\n')
        for i, line in enumerate(lines):
            await message_box.type(line, delay=10)
            if i < len(lines) - 1:
                await page.keyboard.press('Shift+Enter')
        await asyncio.sleep(0.5)

        # Send
        await page.keyboard.press('Enter')
        await asyncio.sleep(1)

        # Success — update state
        _connection_state["logged_in"] = True
        _connection_state["last_check"] = time.time()
        _connection_state["last_success"] = time.time()
        _connection_state["consecutive_failures"] = 0

        return {"success": True, "error": None}

    except Exception as e:
        print(f"WhatsApp send failed: {type(e).__name__}: {e}")
        _connection_state["consecutive_failures"] += 1

        # Auto-retry on unexpected errors
        if retry_count < 2:
            print(f"[WA] Auto-retry {retry_count + 1}/2 after error")
            await _reset_browser()
            await asyncio.sleep(2)
            return await _async_send_message_to_group(group_name, message, retry_count + 1)

        return {"success": False, "error": str(e)}


def send_message_to_group(group_name, message):
    """Send a message to a WhatsApp group (sync wrapper)."""
    return _run_async(_async_send_message_to_group(group_name, message))


async def _reset_browser():
    """Close and clear the current browser context so next call gets a fresh one."""
    global _playwright, _context, _page
    try:
        if _context:
            await _context.close()
    except:
        pass
    try:
        if _playwright:
            await _playwright.stop()
    except:
        pass
    _playwright = None
    _context = None
    _page = None


async def _async_navigate_to_whatsapp(page, max_retries=3):
    """Navigate to WhatsApp Web with retry logic for network errors."""
    last_error = None
    for attempt in range(max_retries):
        try:
            await page.goto('https://web.whatsapp.com', wait_until='domcontentloaded', timeout=30000)
            return True
        except Exception as e:
            last_error = e
            err_str = str(e)
            # Only retry on network errors, not on other issues
            is_network = any(kw in err_str.lower() for kw in [
                'connection_reset', 'connection_refused', 'net::err',
                'timeout', 'network', 'socket', 'dns', 'resolve',
                'err_connection', 'err_name', 'err_internet'
            ])
            if is_network and attempt < max_retries - 1:
                delay = 3 * (attempt + 1)  # 3s, 6s, 9s
                print(f"[WA] Network error navigating to WhatsApp (attempt {attempt+1}/{max_retries}), retrying in {delay}s...")
                await asyncio.sleep(delay)
                # Try killing stale browsers between retries
                _kill_stale_browsers()
            else:
                break
    print(f"[WA] Failed to navigate to WhatsApp after {max_retries} attempts: {last_error}")
    return False


async def _async_reconnect():
    """Full reconnect: reset browser, launch fresh, navigate to WhatsApp Web."""
    await _reset_browser()
    _kill_stale_browsers()
    await asyncio.sleep(1)
    try:
        _, _, page = await _get_browser()

        # Navigate with retry logic
        navigated = await _async_navigate_to_whatsapp(page, max_retries=3)
        if not navigated:
            _connection_state["logged_in"] = False
            _connection_state["last_check"] = time.time()
            return {"ok": False, "logged_in": False, "error": "Cannot reach web.whatsapp.com. Check your internet connection."}

        await asyncio.sleep(4)
        try:
            await page.wait_for_selector('#pane-side', timeout=15000)
            _connection_state["logged_in"] = True
            _connection_state["last_check"] = time.time()
            return {"ok": True, "logged_in": True}
        except:
            try:
                qr = await page.query_selector('[data-icon="qr-code"]')
                if qr:
                    _connection_state["logged_in"] = False
                    _connection_state["last_check"] = time.time()
                    return {"ok": True, "logged_in": False, "message": "QR code shown — please scan"}
            except:
                pass

            # Check for disconnected banner with a "Reconnect" button
            try:
                reconnect_btn = await page.query_selector('[data-testid="alert-phone-disconnected"] button')
                if reconnect_btn:
                    print("[WA] Clicking reconnect button on disconnected banner")
                    await reconnect_btn.click()
                    await asyncio.sleep(5)
                    try:
                        await page.wait_for_selector('#pane-side', timeout=15000)
                        _connection_state["logged_in"] = True
                        _connection_state["last_check"] = time.time()
                        return {"ok": True, "logged_in": True, "message": "Reconnected successfully"}
                    except:
                        pass
            except:
                pass

            _connection_state["logged_in"] = False
            _connection_state["last_check"] = time.time()
            return {"ok": True, "logged_in": False, "message": "WhatsApp loaded but not logged in"}
    except Exception as e:
        _connection_state["logged_in"] = False
        _connection_state["last_check"] = time.time()
        return {"ok": False, "error": str(e)}


def reconnect():
    """Force reconnect WhatsApp Web (sync wrapper)."""
    return _run_async(_async_reconnect())


async def _async_setup_login():
    """Async setup: open visible browser for QR code scan."""
    _kill_stale_browsers()
    os.makedirs(PROFILE_DIR, exist_ok=True)

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,
        ignore_https_errors=True,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-extensions',
            '--no-first-run',
            '--window-position=200,100',
            '--window-size=1000,800',
        ]
    )

    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto('https://web.whatsapp.com')

    print("\n" + "="*60)
    print("WHATSAPP WEB LOGIN")
    print("="*60)
    print("1. Scan the QR code with your WhatsApp mobile app")
    print("2. Wait until you see your chats loaded")
    print("3. Then close this browser window")
    print("="*60 + "\n")

    try:
        await page.wait_for_selector('#pane-side', timeout=120000)
        print("\nLogin successful! You can close the browser now.")
        print("  The session will be saved for future use.\n")
        _connection_state["logged_in"] = True
        _connection_state["last_check"] = time.time()
        _connection_state["last_success"] = time.time()
        await asyncio.sleep(3)
    except:
        print("\nLogin timeout. Please try again.\n")

    await context.close()
    await pw.stop()


def setup_login():
    """Open WhatsApp Web for initial login (QR code scan)."""
    asyncio.run(_async_setup_login())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_login()
    elif len(sys.argv) > 2 and sys.argv[1] == "send":
        group = sys.argv[2]
        msg = sys.argv[3] if len(sys.argv) > 3 else "Test message from Barcode Tracker"
        result = send_message_to_group(group, msg)
        print(f"Result: {result}")
    else:
        print("Usage:")
        print("  python whatsapp_sender.py setup          # Login to WhatsApp Web")
        print("  python whatsapp_sender.py send <group> <message>  # Send message")
