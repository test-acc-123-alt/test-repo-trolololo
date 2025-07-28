#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Instagram profile logger (CI-robust, headless, with graceful fallbacks)

Behavior
--------
- Opens an Instagram profile in headless Chrome (mobile emulation).
- Extracts profile picture URL + followers/following.
- Saves the profile picture only when it changes (normalized URL).
- Appends a row to profile_log.csv:
    timestamp, username, followers, following, is_picture_updated
- Creates debug artifacts on failures (HTML + screenshot).

CI defaults
-----------
- Does NOT fail the job if it can't find a picture (STRICT=0 by default).
  Set STRICT=1 to revert to failing behavior.
- Honors CHROME_PATH and CHROMEDRIVER_PATH from setup-chrome@v2.

Env vars
--------
- IG_USERNAME     : Instagram handle to scrape (default: "zlamp_a")
- STRICT          : "1" => raise on missing picture; otherwise continue (default "0")
- CHROME_PATH     : path to Chrome binary (set by setup-chrome)
- CHROMEDRIVER_PATH: path to chromedriver (set by setup-chrome)
"""

import os
import re
import csv
import time
import shutil
import html
import json
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import urlparse, urlunparse

import requests

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from webdriver_manager.chrome import ChromeDriverManager  # optional fallback
except Exception:
    ChromeDriverManager = None

# -------------------- Configuration --------------------
LAST_PIC_FILE = "last_pic_url.txt"
PIC_DIR = "profile_pics"
LOG_FILE = "profile_log.csv"
DEBUG_DIR = "debug_artifacts"

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

# Instagram JSON (best-effort) fallback headers
IG_JSON_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "application/json",
    "Referer": "https://www.instagram.com/",
    # Common web app id used by instagram.com (may change; best-effort only)
    "X-IG-App-ID": "936619743392459",
}


# -------------------- Utility helpers --------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """Strip query & fragment to avoid cache-busting false positives."""
    if not url:
        return url
    p = urlparse(url)
    return urlunparse(p._replace(query="", fragment=""))


def load_last_pic_url() -> Optional[str]:
    if os.path.exists(LAST_PIC_FILE):
        with open(LAST_PIC_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None


def save_last_pic_url(url: str) -> None:
    with open(LAST_PIC_FILE, "w", encoding="utf-8") as f:
        f.write(url)


def download_image(url: str, filename: str) -> str:
    ensure_dir(PIC_DIR)
    path = os.path.join(PIC_DIR, filename)
    with requests.get(url, stream=True, timeout=45, headers={"User-Agent": MOBILE_UA}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
    return path


def log_to_csv(entry: dict) -> None:
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=["timestamp", "username", "followers", "following", "is_picture_updated"],
        )
        if is_new:
            writer.writeheader()
        writer.writerow(entry)


# -------------------- Browser bootstrap --------------------
def _select_chrome_binary() -> Optional[str]:
    for key in ("CHROME_PATH", "CHROME_BIN"):
        v = os.environ.get(key)
        if v and os.path.exists(v):
            return v
    # Common fallbacks
    for cand in [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]:
        if os.path.exists(cand):
            return cand
    hit = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
    return hit


def _select_chromedriver_path() -> Optional[str]:
    envp = os.environ.get("CHROMEDRIVER_PATH")
    if envp and os.path.exists(envp):
        return envp
    hit = shutil.which("chromedriver")
    if hit:
        return hit
    if ChromeDriverManager:
        try:
            return ChromeDriverManager().install()
        except Exception:
            return None
    return None


def _build_driver() -> webdriver.Chrome:
    chrome_path = _select_chrome_binary()
    if not chrome_path:
        raise RuntimeError("Chrome binary not found. Ensure CHROME_PATH is set.")

    options = Options()
    options.binary_location = chrome_path

    # Mobile emulation
    options.add_experimental_option("mobileEmulation", {"deviceName": "iPhone X"})

    # Headless stability flags
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=390,844")
    options.add_argument("--lang=en-US,en")
    options.add_argument(f"--user-agent={MOBILE_UA}")
    options.add_argument("--remote-debugging-port=9222")

    chromedriver_path = _select_chromedriver_path()
    service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()

    print(f"[debug] Using Chrome binary: {chrome_path}")
    if chromedriver_path:
        print(f"[debug] Using chromedriver: {chromedriver_path}")

    return webdriver.Chrome(service=service, options=options)


# -------------------- Selenium helpers --------------------
def _ready(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass


def _dismiss_interstitials(driver):
    # Cookie banners
    texts = [
        "Only allow essential cookies",
        "Allow all cookies",
        "Accept all",
        "Allow essential cookies",
        "Accept",
        "OK",
    ]
    for t in texts:
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(., '{t}')]"))
            )
            btn.click()
            time.sleep(0.2)
            break
        except Exception:
            pass

    # "Open Instagram" overlays
    for t in ["Open Instagram", "Open app", "Open the app"]:
        try:
            el = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, f"//*[self::div or self::a or self::span or self::button][contains(., '{t}')]"))
            )
            el.click()
            time.sleep(0.4)
            break
        except Exception:
            pass


def _extract_og_image_from_html(html_text: str) -> Optional[str]:
    # property="og:image"
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


def _extract_profile_pic_from_jsonish(html_text: str) -> Optional[str]:
    # e.g. "profile_pic_url_hd":"https:\/\/..."
    m = re.search(r'"profile_pic_url_hd"\s*:\s*"([^"]+)"', html_text)
    if m:
        return html.unescape(m.group(1).replace("\\/", "/"))
    m = re.search(r'"profile_pic_url"\s*:\s*"([^"]+)"', html_text)
    if m:
        return html.unescape(m.group(1).replace("\\/", "/"))
    return None


def _extract_follow_counts_from_meta(html_text: str) -> Tuple[Optional[str], Optional[str]]:
    # Try the meta description: "... followers, ... following, ..."
    dm = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html_text,
        re.IGNORECASE,
    )
    if not dm:
        return None, None
    desc = html.unescape(dm.group(1))
    fol = re.search(r"([\d,.]+)\s+followers", desc, re.IGNORECASE)
    ing = re.search(r"([\d,.]+)\s+following", desc, re.IGNORECASE)
    followers = fol.group(1).replace(",", "") if fol else None
    following = ing.group(1).replace(",", "") if ing else None
    return followers, following


def _best_img_from_header(driver) -> Optional[str]:
    """
    Heuristic: take the largest <img> inside <header>.
    Works when alt text isn't stable/localized.
    """
    try:
        header = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.TAG_NAME, "header")))
    except Exception:
        return None

    imgs = header.find_elements(By.CSS_SELECTOR, "img[src]")
    best_src = None
    best_w = -1
    for img in imgs:
        try:
            src = img.get_attribute("src") or ""
            if not src:
                continue
            # Prefer likely CDN images
            if not any(k in src for k in ("instagram", "cdninstagram", "fbcdn", "/v/")):
                continue
            w = driver.execute_script("return arguments[0].naturalWidth || 0", img)
            if w is None:
                w = 0
            if w > best_w:
                best_w = int(w)
                best_src = src
        except Exception:
            continue

    return best_src


def _get_profile_img_src_dom(driver) -> Optional[str]:
    # First, look for explicit "profile picture" alts (EN and variants)
    selectors = [
        "img[alt$='profile picture']",
        "img[alt*='profile picture']",
        "header img[alt$='profile picture']",
        "header img[alt*='profile picture']",
    ]
    end = time.time() + 12
    while time.time() < end:
        for sel in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    src = els[0].get_attribute("src")
                    if src:
                        return src
            except Exception:
                pass
        time.sleep(0.3)

    # Heuristic: largest img in header
    src = _best_img_from_header(driver)
    if src:
        return src

    # Meta tags via DOM
    for sel in ['meta[property="og:image"]', 'meta[name="og:image"]', 'meta[property="og:image:secure_url"]']:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            content = el.get_attribute("content")
            if content:
                return content
        except Exception:
            pass

    # Regex from page_source
    html_src = driver.page_source or ""
    return _extract_og_image_from_html(html_src) or _extract_profile_pic_from_jsonish(html_src)


# -------------------- HTTP fallbacks --------------------
def _fetch_profile_html(username: str) -> Optional[str]:
    try:
        r = requests.get(f"https://www.instagram.com/{username}/", headers=REQ_HEADERS, timeout=45)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    return None


def _fetch_profile_json(username: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Best-effort unauthenticated JSON. May be blocked by IG sometimes.
    Returns (pic_url, followers, following).
    """
    try:
        url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
        r = requests.get(url, headers=IG_JSON_HEADERS, timeout=45)
        if r.status_code == 200:
            data = r.json()
            user = (data.get("data") or {}).get("user") or {}
            pic = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
            followers = (
                (user.get("edge_followed_by") or {}).get("count")
                or user.get("follower_count")
            )
            following = (
                (user.get("edge_follow") or {}).get("count")
                or user.get("following_count")
            )
            # Cast to str for CSV consistency
            followers = str(followers) if followers is not None else None
            following = str(following) if following is not None else None
            return pic, followers, following
    except Exception:
        pass
    return None, None, None


# -------------------- Main scrape --------------------
def scrape_and_log(username: str, strict: bool = False) -> dict:
    ensure_dir(DEBUG_DIR)

    url = f"https://www.instagram.com/{username}/"
    pic_url: Optional[str] = None
    followers: Optional[str] = None
    following: Optional[str] = None

    driver = None
    try:
        driver = _build_driver()
    except Exception as e:
        print(f"[warn] Could not start Selenium Chrome: {e}")

    page_html = None

    if driver:
        try:
            driver.get(url)
            _ready(driver, 20)
            _dismiss_interstitials(driver)
            _ready(driver, 10)

            # Try all DOM strategies
            pic_url = _get_profile_img_src_dom(driver)

            # Followers/Following from DOM meta
            try:
                m = driver.find_element(By.CSS_SELECTOR, 'meta[name="description"]')
                desc = m.get_attribute("content") or ""
                fol = re.search(r"([\d,.]+)\s+followers", desc, re.IGNORECASE)
                ing = re.search(r"([\d,.]+)\s+following", desc, re.IGNORECASE)
                followers = followers or (fol.group(1).replace(",", "") if fol else None)
                following = following or (ing.group(1).replace(",", "") if ing else None)
            except Exception:
                pass

            # Hold page_source for further parsing if needed
            page_html = driver.page_source or ""

            if not pic_url:
                # Try regex/meta on page_source
                pic_url = _extract_og_image_from_html(page_html) or _extract_profile_pic_from_jsonish(page_html)

        finally:
            # Save artifacts if we failed so far (helps diagnose CI content)
            if not pic_url:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    if page_html is None:
                        page_html = driver.page_source or ""
                except Exception:
                    pass
                try:
                    with open(os.path.join(DEBUG_DIR, f"page_source_{ts}.html"), "w", encoding="utf-8") as f:
                        f.write(page_html or "")
                except Exception:
                    pass
                try:
                    driver.save_screenshot(os.path.join(DEBUG_DIR, f"screenshot_{ts}.png"))
                except Exception:
                    pass
            try:
                driver.quit()
            except Exception:
                pass

    # HTTP Fallbacks (raw HTML + JSON)
    if not (pic_url and followers and following):
        if not page_html:
            page_html = _fetch_profile_html(username) or ""
        if not pic_url:
            pic_url = _extract_og_image_from_html(page_html) or _extract_profile_pic_from_jsonish(page_html)
        if not (followers and following):
            f1, f2 = _extract_follow_counts_from_meta(page_html or "")
            followers = followers or f1
            following = following or f2

    if not (pic_url and followers and following):
        # Try JSON endpoint as a last resort
        pic2, fol2, ing2 = _fetch_profile_json(username)
        pic_url = pic_url or pic2
        followers = followers or fol2
        following = following or ing2

    # At this point, we might still be missing pic_url on hard guest walls.
    if not pic_url:
        msg = "Could not locate profile picture after all strategies."
        if strict:
            raise RuntimeError(msg)
        else:
            print(f"[warn] {msg} Proceeding without picture update check.")
            # Log with is_picture_updated = 0 and return gracefully.

    # Compare & maybe download
    is_updated = 0
    if pic_url:
        current_norm = normalize_url(pic_url)
        last_norm = load_last_pic_url()
        if current_norm != last_norm:
            # Save the new picture
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"{ts}_{username}_profile.jpg"
            try:
                download_image(pic_url, fname)
                save_last_pic_url(current_norm)
                is_updated = 1
            except Exception as e:
                print(f"[warn] Failed to download image: {e}. Will continue without saving.")

    entry = {
        "timestamp": now_iso(),
        "username": username,
        "followers": followers,
        "following": following,
        "is_picture_updated": is_updated,
    }
    log_to_csv(entry)
    return entry


# -------------------- CLI --------------------
if __name__ == "__main__":
    username = os.environ.get("IG_USERNAME", "zlamp_a").strip()
    strict = os.environ.get("STRICT", "0") == "1"
    try:
        result = scrape_and_log(username, strict=strict)
        print(result)
    except Exception as e:
        # Safety net: only propagate non-zero exit when STRICT=1
        if strict:
            raise
        print(f"[warn] Non-fatal error: {e}")
