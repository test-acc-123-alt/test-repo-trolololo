#!/usr/bin/env python3
"""
Instagram profile scraper (CI-friendly, headless Chrome)

- Works on GitHub Actions with browser-actions/setup-chrome.
- Reads Chrome and ChromeDriver paths from env:
    CHROME_PATH, CHROMEDRIVER_PATH
  and falls back to common locations if not set.
- Extracts profile image URL from <meta property="og:image"> first,
  with multiple resilient fallbacks.
- Parses follower/following/posts from <meta property="og:description">.
- Never fails the CI job unless STRICT=1.
- Always logs a CSV row: scrape.csv

Environment (optional):
  IG_USERNAME         - Instagram username to scrape (default: "zlamp_a")
  CHROME_PATH         - Absolute path to chrome binary
  CHROMEDRIVER_PATH   - Absolute path to chromedriver
  STRICT              - If "1", raise on errors (default: "0")
  DEBUG               - If "1", dump artifacts (HTML & screenshot)

CSV columns:
  timestamp,username,followers,following,posts,picture_url,is_picture_updated
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# -------------------- Utilities -------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def print_debug(msg: str) -> None:
    # Always print with a simple prefix for grep-ability in CI logs.
    print(f"[debug] {msg}")


def coalesce(*values, default=None):
    for v in values:
        if v is not None:
            return v
    return default


def parse_compact_number(s: str) -> Optional[int]:
    """
    Convert strings like '1,234', '1.2k', '3.4m' to an integer.
    Returns None if parsing fails.
    """
    if not s:
        return None
    t = s.strip().lower()
    # Normalize separators
    t = t.replace(" ", "")
    # If it contains k/m/b suffix
    m = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*([kmb])", t)
    if m:
        num = m.group(1).replace(",", ".")
        try:
            val = float(num)
        except ValueError:
            return None
        mul = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2)]
        return int(round(val * mul))
    # Otherwise just digits with separators
    digits = re.sub(r"[.,]", "", t)
    if digits.isdigit():
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def parse_counts_from_og_description(text: str) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Typical og:description looks like:
      "X followers, Y following, Z posts - See Instagram photos and videos from ..."
    This parser is defensive to minor format variations.
    """
    if not text:
        return None, None, None

    # Try a single regex first
    m = re.search(
        r"([\d.,]+[kmb]?)\s+followers.*?([\d.,]+[kmb]?)\s+following.*?([\d.,]+[kmb]?)\s+posts",
        text.lower(),
    )
    if m:
        followers = parse_compact_number(m.group(1))
        following = parse_compact_number(m.group(2))
        posts = parse_compact_number(m.group(3))
        return followers, following, posts

    # Fallback: extract any numbers with hints around them
    followers = following = posts = None

    fm = re.search(r"([\d.,]+[kmb]?)\s+followers", text.lower())
    if fm:
        followers = parse_compact_number(fm.group(1))

    fim = re.search(r"([\d.,]+[kmb]?)\s+following", text.lower())
    if fim:
        following = parse_compact_number(fim.group(1))

    pm = re.search(r"([\d.,]+[kmb]?)\s+posts", text.lower())
    if pm:
        posts = parse_compact_number(pm.group(1))

    return followers, following, posts


def read_last_picture_url(csv_path: Path, username: str) -> Optional[str]:
    if not csv_path.exists():
        return None
    last_url = None
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("username") == username and row.get("picture_url"):
                    last_url = row["picture_url"]
    except Exception:
        return None
    return last_url


def log_to_csv(entry: dict, csv_path: Path) -> None:
    csv_path = csv_path.resolve()
    ensure_dir(csv_path.parent)
    write_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "username",
                "followers",
                "following",
                "posts",
                "picture_url",
                "is_picture_updated",
            ],
        )
        if write_header:
            writer.writeheader()
        writer.writerow(entry)


# -------------------- Selenium setup -------------------- #

def detect_chrome_binary() -> Optional[str]:
    """Pick Chrome binary path from env or common locations."""
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    # Common hostedtoolcache fallback on Actions
    hosted = "/opt/hostedtoolcache/setup-chrome/chrome/stable/x64/chrome"
    if Path(hosted).exists():
        return hosted

    # Typical system locations
    for p in ("/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"):
        if Path(p).exists():
            return p
    return None


def detect_chromedriver() -> Optional[str]:
    env_path = os.environ.get("CHROMEDRIVER_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    hosted = "/opt/hostedtoolcache/setup-chrome/chromedriver/stable/x64/chromedriver"
    if Path(hosted).exists():
        return hosted

    # If on PATH
    from shutil import which
    wh = which("chromedriver")
    return wh


def build_driver(headless: bool = True) -> webdriver.Chrome:
    chrome_path = detect_chrome_binary()
    driver_path = detect_chromedriver()

    if chrome_path:
        print_debug(f"Using Chrome binary: {chrome_path}")
    else:
        print_debug("Chrome binary not found by autodetect; letting Selenium pick.")

    if driver_path:
        print_debug(f"Using chromedriver: {driver_path}")
    else:
        print_debug("chromedriver not found by autodetect; letting Selenium pick.")

    options = Options()
    if chrome_path:
        options.binary_location = chrome_path

    if headless:
        options.add_argument("--headless=new")

    # Hardening flags for CI
    for arg in (
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--window-size=1280,2000",
        "--disable-features=VizDisplayCompositor",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US,en",
    ):
        options.add_argument(arg)

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.set_capability("pageLoadStrategy", "eager")

    service = ChromeService(executable_path=driver_path) if driver_path else ChromeService()
    driver = webdriver.Chrome(service=service, options=options)

    # Pretend to be a real browser a bit more
    try:
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            )
        })
    except Exception:
        pass

    return driver


# -------------------- Scraping logic -------------------- #

@dataclass
class ScrapeResult:
    username: str
    followers: Optional[int]
    following: Optional[int]
    posts: Optional[int]
    picture_url: Optional[str]


def try_click_cookie_banner(driver: webdriver.Chrome, timeout: int = 5) -> None:
    """
    Best-effort click to accept cookies if banner shows up.
    Non-fatal if not present.
    """
    try:
        w = WebDriverWait(driver, timeout)
        # Common button labels on Instagram cookie modals
        candidates = [
            "//button[.//text()[contains(., 'Allow all') or contains(., 'Accept all')]]",
            "//button[normalize-space()='Allow essential and optional cookies']",
            "//button[normalize-space()='Only allow essential cookies']",
            "//button[normalize-space()='Accept All']",
            "//div[@role='dialog']//button[contains(., 'Accept')]",
        ]
        for xp in candidates:
            try:
                el = w.until(EC.presence_of_element_located((By.XPATH, xp)))
                if el.is_displayed():
                    el.click()
                    time.sleep(0.5)
                    break
            except Exception:
                continue
    except Exception:
        pass


def get_meta_content(driver: webdriver.Chrome, prop: str, timeout: int = 10) -> Optional[str]:
    """
    Return the content of <meta property="{prop}" content="..."> if present.
    """
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, f'meta[property="{prop}"]'))
        )
    except Exception:
        # Continue anyway; we'll try JS or other fallbacks
        pass

    # JS first (tends to be more reliable across drivers)
    try:
        val = driver.execute_script(
            "const m=document.querySelector('meta[property=\"arguments[0]\"]');"
            "return m?m.getAttribute('content'):null;",
            prop
        )
        if val:
            return val
    except Exception:
        pass

    # Direct lookup
    try:
        meta = driver.find_element(By.CSS_SELECTOR, f'meta[property="{prop}"]')
        return meta.get_attribute("content")
    except Exception:
        return None


def find_profile_image_url(driver: webdriver.Chrome) -> Optional[str]:
    """
    Primary: og:image meta
    Fallbacks: various <img> selectors commonly used by Instagram
    """
    url = get_meta_content(driver, "og:image", timeout=12)
    if url:
        return url

    # Fallback selectors known to appear on public profiles
    selectors = [
        'img[alt$="profile picture"]',
        'img[alt*="Profile picture"]',
        'img[decoding][sizes][srcset]',
        'img[style*="border-radius"]',
        'header img',
        'img[alt][src][crossorigin]',
    ]
    for sel in selectors:
        try:
            el = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            src = el.get_attribute("src") or el.get_attribute("data-src")
            if src and "data:" not in src:
                return src
        except Exception:
            continue

    return None


def scrape_profile(username: str, debug: bool = False) -> ScrapeResult:
    profile_url = f"https://www.instagram.com/{username.strip().lstrip('@').rstrip('/')}/"

    driver = build_driver(headless=True)
    try:
        driver.get(profile_url)
        # Best-effort cookie click
        try_click_cookie_banner(driver, timeout=5)

        # Give the page a moment if needed
        time.sleep(1.0)

        # Counts via og:description
        og_desc = get_meta_content(driver, "og:description", timeout=10)
        followers, following, posts = parse_counts_from_og_description(og_desc or "")

        # Picture URL resolution
        pic_url = find_profile_image_url(driver)

        # Optional artifacts
        if debug:
            art_dir = Path("artifacts")
            ensure_dir(art_dir)
            try:
                (art_dir / "page.html").write_text(driver.page_source, encoding="utf-8")
            except Exception:
                pass
            try:
                driver.save_screenshot(str(art_dir / "screenshot.png"))
            except Exception:
                pass

        return ScrapeResult(
            username=username,
            followers=followers,
            following=following,
            posts=posts,
            picture_url=pic_url,
        )
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# -------------------- Orchestration -------------------- #

def scrape_and_log(username: str, strict: bool = False, debug: bool = False) -> dict:
    csv_path = Path("scrape.csv")
    prev_pic = read_last_picture_url(csv_path, username)

    result = scrape_profile(username, debug=debug)

    is_updated = 0
    if result.picture_url and result.picture_url != prev_pic:
        is_updated = 1

    row = {
        "timestamp": now_iso(),
        "username": result.username,
        "followers": result.followers if result.followers is not None else "",
        "following": result.following if result.following is not None else "",
        "posts": result.posts if result.posts is not None else "",
        "picture_url": result.picture_url or "",
        "is_picture_updated": is_updated,
    }

    log_to_csv(row, csv_path)

    # If strict, raise on key failures (e.g., no picture), but only after logging
    if strict and not result.picture_url:
        raise RuntimeError("Could not locate profile picture on the page")

    return row


# -------------------- Entrypoint -------------------- #

if __name__ == "__main__":
    username = os.environ.get("IG_USERNAME", "zlamp_a").strip()
    strict = os.environ.get("STRICT", "0") == "1"
    debug = os.environ.get("DEBUG", "0") == "1"

    try:
        out = scrape_and_log(username, strict=strict, debug=debug)
        print(out)
        sys.exit(0)
    except Exception as e:
        # Soft-fail by default so CI stays green
        if strict:
            raise
        print(f"[warn] Non-fatal error: {e}")
        # Ensure a row exists even on failure
        fallback_row = {
            "timestamp": now_iso(),
            "username": username,
            "followers": "",
            "following": "",
            "posts": "",
            "picture_url": "",
            "is_picture_updated": 0,
        }
        log_to_csv(fallback_row, Path("scrape.csv"))
        print(fallback_row)
        sys.exit(0)
