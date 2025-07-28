#!/usr/bin/env python3
"""
Instagram profile logger (headless/mobile-friendly)

What it does
------------
- Loads a public Instagram profile page as a mobile browser (iPhone X).
- Extracts the profile picture URL, followers, following.
- Saves a timestamped copy of the profile picture ONLY if it changed
  since the last run (based on a normalized URL comparison).
- Appends a row to profile_log.csv with: timestamp, username, followers, following, is_picture_updated.

Works locally and on GitHub Actions.

CI notes
--------
On GitHub Actions, install Chrome + Chromedriver (e.g. with browser-actions/setup-chrome@v2)
and pass their paths via env:
  CHROME_PATH: path to the chrome binary (e.g. /usr/bin/google-chrome)
  CHROMEDRIVER_PATH: path to chromedriver binary

This script will also auto-detect common Chrome/Chromium locations if env vars are not set.
"""

import os
import re
import csv
import time
import shutil
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
    r = requests.get(url, stream=True, timeout=30)
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


# ---------- Page helpers ----------
def _try_click_cookies(driver) -> None:
    """
    Best-effort click of EU cookie dialogs if they appear.
    """
    for text in [
        "Only allow essential cookies",
        "Allow all cookies",
        "Accept all",
        "Allow essential cookies",
        "Accept",
    ]:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(., '{text}')]"))
            )
            btn.click()
            time.sleep(0.3)
            return
        except Exception:
            pass


def _get_profile_img_src(driver) -> str | None:
    """
    Try multiple selectors that commonly work on the mobile/guest view of Instagram.
    Fallback to og:image meta if <img> is not directly accessible.
    """
    selectors = [
        "img[alt$='profile picture']",
        "img[alt*='profile picture']",
        "header img[alt$='profile picture']",
        "header a img[alt$='profile picture']",
        "header a img",
    ]
    end = time.time() + 12
    while time.time() < end:
        for sel in selectors:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                src = els[0].get_attribute("src")
                if src:
                    return src
        time.sleep(0.4)

    # Fallback: <meta property="og:image" content="...">
    try:
        og = driver.find_element(By.CSS_SELECTOR, "meta[property='og:image']")
        content = og.get_attribute("content")
        if content:
            return content
    except Exception:
        pass
    return None


def _get_follow_counts(driver) -> tuple[str | None, str | None]:
    """
    Followers and following:
    - Prefer the two header buttons ("xx followers", "yy following") on mobile profiles.
    - If not available (interstitial/layout/locale), fallback to <meta name="description"> parsing.
    """
    # Primary: header buttons
    try:
        buttons = WebDriverWait(driver, 8).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "header section button"))
        )
        if len(buttons) >= 2:
            followers_text, following_text = buttons[0].text, buttons[1].text

            def num(txt: str | None) -> str | None:
                if not txt:
                    return None
                m = re.search(r"[\d,.]+", txt.replace(",", ""))
                return m.group(0) if m else None

            return num(followers_text), num(following_text)
    except Exception:
        pass

    # Fallback: meta description (e.g. "105 followers, 128 following, 6 posts – ...")
    try:
        desc = driver.find_element(By.CSS_SELECTOR, "meta[name='description']").get_attribute("content") or ""
        fol = re.search(r"([\d,.]+)\s+followers", desc)
        ing = re.search(r"([\d,.]+)\s+following", desc)
        followers = fol.group(1).replace(",", "") if fol else None
        following = ing.group(1).replace(",", "") if ing else None
        return followers, following
    except Exception:
        return None, None


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
    options.add_argument("--window-size=390,844")  # ~iPhone 12 Pro portrait
    options.add_argument("--lang=en-US,en")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )

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
    """
    url = f"https://www.instagram.com/{username}/"
    driver = _build_driver()

    try:
        driver.get(url)
        _try_click_cookies(driver)

        # Get profile image URL (robust)
        current_pic_url_raw = _get_profile_img_src(driver)
        if not current_pic_url_raw:
            raise RuntimeError("Could not locate profile picture on the page")

        # Normalize to avoid query-based cache busters triggering false updates
        current_pic_url_norm = normalize_url(current_pic_url_raw)
        last_pic_url_norm = load_last_pic_url()

        if current_pic_url_norm != last_pic_url_norm:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{ts}_{username}_profile.jpg"
            download_image(current_pic_url_raw, filename)
            save_last_pic_url(current_pic_url_norm)
            is_updated = 1
        else:
            is_updated = 0

        # Followers / Following with fallback
        followers, following = _get_follow_counts(driver)

        entry = {
            "timestamp": datetime.now().isoformat(),
            "username": username,
            "followers": followers,
            "following": following,
            "is_picture_updated": is_updated,
        }
        log_to_csv(entry)
        return entry

    finally:
        driver.quit()


if __name__ == "__main__":
    # Change the username here or supply via env/argument parsing as needed
    username = os.environ.get("IG_USERNAME", "zlamp_a")
    result = scrape_and_log(username)
    print(result)
