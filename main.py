#!/usr/bin/env python3
"""
Instagram profile logger (headless/mobile-friendly, CI-robust)

What it does
------------
- Loads an Instagram profile page as a mobile browser (iPhone X).
- Extracts the profile picture URL, followers, following.
- Saves a timestamped copy of the profile picture ONLY if it changed
  since the last run (based on a normalized URL comparison).
- Appends a row to profile_log.csv with: timestamp, username, followers, following, is_picture_updated.

CI notes
--------
On GitHub Actions, install Chrome + Chromedriver (e.g. with browser-actions/setup-chrome@v2)
and pass their paths via env:
  CHROME_PATH: path to the chrome binary
  CHROMEDRIVER_PATH: path to chromedriver binary

The script also auto-detects Chrome/Chromium if env vars are not set.
"""

import os
import re
import csv
import time
import shutil
import html
import requests
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from webdriver_manager.chrome import ChromeDriverManager  # fallback if no system chromedriver
except Exception:
    ChromeDriverManager = None

# --- Configuration ---
LAST_PIC_FILE = "last_pic_url.txt"
PIC_DIR = "profile_pics"         # images saved here
LOG_FILE = "profile_log.csv"     # CSV output

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)
REQ_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------- Utilities ----------
def load_last_pic_url() -> str | None:
    if os.path.exists(LAST_PIC_FILE):
        with open(LAST_PIC_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_pic_url(url: str) -> None:
    with open(LAST_PIC_FILE, "w") as f:
        f.write(url)


def normalize_url(url: str) -> str:
    """
    Normalize CDN URLs by removing query strings and fragments so cache-busters
    don't cause false positives for "changed" images.
    """
    parsed = urlparse(url)
    cleaned = parsed._replace(query="", fragment="")
    return urlunparse(cleaned)


def download_image(url: str, filename: str) -> str:
    """
    Download image to PIC_DIR/filename. Returns full path.
    """
    os.makedirs(PIC_DIR, exist_ok=True)
    path = os.path.join(PIC_DIR, filename)
    r = requests.get(url, stream=True, timeout=30, headers={"User-Agent": MOBILE_UA})
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return path


def log_to_csv(entry: dict) -> None:
    """
    Append a row to the CSV, creating headers on first write.
    """
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=[
                "timestamp",
                "username",
                "followers",
                "following",
                "is_picture_updated",
            ],
        )
        if is_new:
            writer.writeheader()
        writer.writerow(entry)


# ---------- Page helpers (Selenium) ----------
def _try_click_cookies_and_open(driver) -> None:
    """
    Best-effort click of EU cookie dialogs and "Open Instagram" interstitials if they appear.
    """
    # Cookie buttons
    for text in [
        "Only allow essential cookies",
        "Allow all cookies",
        "Accept all",
        "Allow essential cookies",
        "Accept",
    ]:
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(., '{text}')]"))
            )
            btn.click()
            time.sleep(0.2)
            break
        except Exception:
            pass

    # "Open Instagram" / "Open app" surfaces sometimes gate the profile
    for text in ["Open Instagram", "Open app", "Open the app"]:
        try:
            el = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, f"//*[self::div or self::a or self::span or self::button][contains(., '{text}')]"))
            )
            el.click()
            time.sleep(0.4)
            break
        except Exception:
            pass


def _wait_dom_ready(driver, timeout=10):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass


def _get_profile_img_src_dom(driver) -> str | None:
    """
    Try multiple selectors that commonly work on the mobile/guest view of Instagram.
    Also look for meta og:image via DOM and finally regex the page_source.
    """
    # 1) direct <img ... profile picture>
    selectors = [
        "img[alt$='profile picture']",
        "img[alt*='profile picture']",
        "header img[alt$='profile picture']",
        "header a img[alt$='profile picture']",
        "header a img",
    ]
    end = time.time() + 10
    while time.time() < end:
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                src = els[0].get_attribute("src")
                if src:
                    return src
        time.sleep(0.3)

    # 2) meta og:image via DOM
    for meta_sel in [
        'meta[property="og:image"]',
        'meta[name="og:image"]',
        'meta[property="og:image:secure_url"]',
    ]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, meta_sel)
            content = el.get_attribute("content")
            if content:
                return content
        except Exception:
            pass

    # 3) regex from page_source for meta og:image
    html_src = driver.page_source or ""
    src = _extract_og_image_from_html(html_src)
    if src:
        return src

    # 4) regex from page_source for JSON key profile_pic_url_hd
    src = _extract_profile_pic_from_json(html_src)
    if src:
        return src

    return None


# ---------- No-Selenium fallbacks ----------
def _fetch_profile_html(username: str) -> str | None:
    try:
        resp = requests.get(f"https://www.instagram.com/{username}/", headers=REQ_HEADERS, timeout=30)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception:
        return None
    return None


def _extract_og_image_from_html(html_text: str) -> str | None:
    # <meta property="og:image" content="...">
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    if m:
        return html.unescape(m.group(1))
    # name="og:image"
    m = re.search(
        r'<meta[^>]+name=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    if m:
        return html.unescape(m.group(1))
    # secure_url
    m = re.search(
        r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    if m:
        return html.unescape(m.group(1))
    return None


def _extract_profile_pic_from_json(html_text: str) -> str | None:
    """
    Look for JSON key commonly embedded in page: "profile_pic_url_hd":"https:\/\/...jpg"
    """
    m = re.search(r'"profile_pic_url_hd"\s*:\s*"([^"]+)"', html_text)
    if m:
        val = m.group(1)
        val = val.replace("\\/", "/")
        return html.unescape(val)
    # sometimes just "profile_pic_url"
    m = re.search(r'"profile_pic_url"\s*:\s*"([^"]+)"', html_text)
    if m:
        val = m.group(1).replace("\\/", "/")
        return html.unescape(val)
    return None


def _extract_follow_counts_from_html(html_text: str) -> tuple[str | None, str | None]:
    """
    Parse followers/following from meta description:
      e.g. '105 followers, 128 following, 6 posts – ...'
    """
    desc = None
    dm = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    if dm:
        desc = html.unescape(dm.group(1))
    if not desc:
        return None, None

    fol = re.search(r"([\d,.]+)\s+followers", desc, re.IGNORECASE)
    ing = re.search(r"([\d,.]+)\s+following", desc, re.IGNORECASE)
    followers = fol.group(1).replace(",", "") if fol else None
    following = ing.group(1).replace(",", "") if ing else None
    return followers, following


# ---------- Browser selection ----------
def _select_chrome_binary() -> str | None:
    """
    Choose a Chrome/Chromium binary:
    - Prefer env CHROME_PATH (from setup-chrome action) or CHROME_BIN.
    - Then check common locations, including Snap chromium.
    """
    for env_name in ("CHROME_PATH", "CHROME_BIN"):
        v = os.environ.get(env_name)
        if v and os.path.exists(v):
            return v

    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "google-chrome",
        "chromium-browser",
        "chromium",
    ]
    for c in candidates:
        if os.path.basename(c) == c:
            p = shutil.which(c)
            if p:
                return p
        else:
            if os.path.exists(c):
                return c
    return None


def _select_chromedriver_path() -> str | None:
    """
    Locate chromedriver from env/Path; fallback to webdriver_manager if needed.
    """
    env_path = os.environ.get("CHROMEDRIVER_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    p = shutil.which("chromedriver")
    if p:
        return p

    if ChromeDriverManager:
        try:
            return ChromeDriverManager().install()
        except Exception:
            return None

    return None


def _build_driver() -> webdriver.Chrome:
    """
    Build a Chrome webdriver configured for CI/headless and mobile emulation.
    """
    options = Options()

    # Mobile emulation (iPhone X)
    options.add_experimental_option("mobileEmulation", {"deviceName": "iPhone X"})

    # Headless & CI-stable flags
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--window-size=390,844")  # ~iPhone portrait
    options.add_argument("--lang=en-US,en")
    options.add_argument(f"--user-agent={MOBILE_UA}")

    chrome_binary = _select_chrome_binary()
    if not chrome_binary:
        raise RuntimeError(
            "Could not locate a Chrome/Chromium binary. "
            "Set CHROME_PATH (preferred) or CHROME_BIN to the browser path."
        )
    options.binary_location = chrome_binary
    print(f"[debug] Using Chrome binary: {chrome_binary}")

    chromedriver_path = _select_chromedriver_path()
    service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
    if chromedriver_path:
        print(f"[debug] Using chromedriver: {chromedriver_path}")

    return webdriver.Chrome(service=service, options=options)


# ---------- Main logic ----------
def scrape_and_log(username: str) -> dict:
    """
    Visit the profile, capture followers/following and profile image state, log to CSV.
    With robust fallbacks so CI doesn’t fail if Selenium can’t see the picture.
    """
    url = f"https://www.instagram.com/{username}/"

    # Try Selenium first (helps with interstitials/cookies)
    pic_url = None
    followers = None
    following = None

    try:
        driver = _build_driver()
    except Exception as e:
        print(f"[warn] Could not start Selenium Chrome: {e}")
        driver = None

    if driver:
        try:
            driver.get(url)
            _wait_dom_ready(driver, 10)
            _try_click_cookies_and_open(driver)
            _wait_dom_ready(driver, 6)

            # Try all DOM strategies
            pic_url = _get_profile_img_src_dom(driver)
            if not pic_url:
                print("[warn] Selenium could not find profile picture; will try HTTP fallback.")

            # Followers/Following via DOM/meta if possible
            if not (followers and following):
                try:
                    # Prefer header buttons
                    buttons = driver.find_elements(By.CSS_SELECTOR, "header section button")
                    if len(buttons) >= 2:
                        def num(txt):
                            if not txt:
                                return None
                            m = re.search(r"[\d,.]+", txt.replace(",", ""))
                            return m.group(0) if m else None
                        followers = followers or num(buttons[0].text)
                        following = following or num(buttons[1].text)
                except Exception:
                    pass

                if not (followers and following):
                    try:
                        m = driver.find_element(By.CSS_SELECTOR, 'meta[name="description"]')
                        desc = m.get_attribute("content") or ""
                        fol = re.search(r"([\d,.]+)\s+followers", desc, re.IGNORECASE)
                        ing = re.search(r"([\d,.]+)\s+following", desc, re.IGNORECASE)
                        followers = followers or (fol.group(1).replace(",", "") if fol else None)
                        following = following or (ing.group(1).replace(",", "") if ing else None)
                    except Exception:
                        pass

        finally:
            driver.quit()

    # HTTP fallback for picture and counts (robust on CI)
    if not pic_url or not (followers and following):
        html_text = _fetch_profile_html(username)
        if html_text:
            if not pic_url:
                pic_url = (
                    _extract_og_image_from_html(html_text)
                    or _extract_profile_pic_from_json(html_text)
                )
            if not (followers and following):
                f1, f2 = _extract_follow_counts_from_html(html_text)
                followers = followers or f1
                following = following or f2

    if not pic_url:
        raise RuntimeError("Could not locate profile picture on the page (Selenium + HTTP fallback failed).")

    # Normalize to avoid query-based cache busters triggering false updates
    current_pic_url_norm = normalize_url(pic_url)
    last_pic_url_norm = load_last_pic_url()

    if current_pic_url_norm != last_pic_url_norm:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{username}_profile.jpg"
        # Use the *raw* (non-normalized) URL for download to keep CDN params if needed
        download_image(pic_url, filename)
        save_last_pic_url(current_pic_url_norm)
        is_updated = 1
    else:
        is_updated = 0

    entry = {
        "timestamp": datetime.now().isoformat(),
        "username": username,
        "followers": followers,
        "following": following,
        "is_picture_updated": is_updated,
    }
    log_to_csv(entry)
    return entry


if __name__ == "__main__":
    username = os.environ.get("IG_USERNAME", "zlamp_a")
    result = scrape_and_log(username)
    print(result)
