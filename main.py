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
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from webdriver_manager.chrome import ChromeDriverManager
except Exception:
    ChromeDriverManager = None

LAST_PIC_FILE = "last_pic_url.txt"
PIC_DIR = "profile_pics"
LOG_FILE = "profile_log.csv"

def load_last_pic_url():
    if os.path.exists(LAST_PIC_FILE):
        with open(LAST_PIC_FILE, "r") as f:
            return f.read().strip()
    return None

def save_last_pic_url(url):
    with open(LAST_PIC_FILE, "w") as f:
        f.write(url)

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned = parsed._replace(query="", fragment="")
    return urlunparse(cleaned)

def download_image(url, filename):
    os.makedirs(PIC_DIR, exist_ok=True)
    path = os.path.join(PIC_DIR, filename)
    r = requests.get(url, stream=True, timeout=30)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return path

def log_to_csv(entry: dict):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            "timestamp", "username", "followers", "following", "is_picture_updated"
        ])
        if is_new:
            writer.writeheader()
        writer.writerow(entry)

def _try_click_cookies(driver):
    for text in [
        "Only allow essential cookies", "Allow all cookies",
        "Accept all", "Allow essential cookies", "Accept"
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
    # fallback to og:image
    try:
        og = driver.find_element(By.CSS_SELECTOR, "meta[property='og:image']")
        content = og.get_attribute("content")
        if content:
            return content
    except Exception:
        pass
    return None

def _get_follow_counts(driver) -> tuple[str | None, str | None]:
    # Prefer header buttons
    try:
        buttons = WebDriverWait(driver, 8).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "header section button"))
        )
        if len(buttons) >= 2:
            followers_text, following_text = buttons[0].text, buttons[1].text
            def num(txt):
                if not txt:
                    return None
                m = re.search(r"[\d,.]+", txt.replace(",", ""))
                return m.group(0) if m else None
            return num(followers_text), num(following_text)
    except Exception:
        pass

    # Fallback: parse meta description
    try:
        desc = driver.find_element(By.CSS_SELECTOR, "meta[name='description']").get_attribute("content") or ""
        fol = re.search(r"([\d,.]+)\s+followers", desc)
        ing = re.search(r"([\d,.]+)\s+following", desc)
        followers = fol.group(1).replace(",", "") if fol else None
        following = ing.group(1).replace(",", "") if ing else None
        return followers, following
    except Exception:
        return None, None

def _select_chrome_binary() -> str | None:
    # 1) Respect CHROME_BIN if provided
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin

    # 2) Common locations/names (we include snap & chrome stable)
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
        # If it's a bare name, which() it; if it looks like a path, check it.
        if os.path.basename(c) == c:
            p = shutil.which(c)
            if p:
                return p
        else:
            if os.path.exists(c):
                return c
    return None

def scrape_and_log(username: str):
    url = f"https://www.instagram.com/{username}/"

    options = Options()
    options.add_experimental_option("mobileEmulation", {"deviceName": "iPhone X"})
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--window-size=390,844")
    options.add_argument("--lang=en-US,en")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )

    chrome_binary = _select_chrome_binary()
    if not chrome_binary:
        raise RuntimeError(
            "Could not locate a Chrome/Chromium binary. "
            "Set CHROME_BIN or install google-chrome/chromium."
        )
    options.binary_location = chrome_binary
    print(f"[debug] Using Chrome binary: {chrome_binary}")

    # Prefer system chromedriver; fallback to webdriver-manager if not present
    driver_path = shutil.which("chromedriver")
    if not driver_path and ChromeDriverManager:
        driver_path = ChromeDriverManager().install()
    service = Service(executable_path=driver_path) if driver_path else Service()
    if driver_path:
        print(f"[debug] Using chromedriver: {driver_path}")

    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(url)
        _try_click_cookies(driver)

        current_pic_url_raw = _get_profile_img_src(driver)
        if not current_pic_url_raw:
            raise RuntimeError("Could not locate profile picture on the page")

        current_pic_url = normalize_url(current_pic_url_raw)
        last_pic_url = load_last_pic_url()

        if current_pic_url != last_pic_url:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{ts}_{username}_profile.jpg"
            download_image(current_pic_url_raw, filename)
            save_last_pic_url(current_pic_url)
            is_updated = 1
        else:
            is_updated = 0

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
    print(scrape_and_log("zlamp_a"))
