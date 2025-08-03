import time
import re
import shutil
import requests
import os
import csv
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
    ChromeDriverManager = None  # We’ll prefer system chromedriver on GitHub

# --- Config ---
LAST_PIC_FILE = "last_pic_url.txt"
PIC_DIR       = "profile_pics"
LOG_FILE      = "profile_log.csv"

# --- Helpers ---
def load_last_pic_url():
    if os.path.exists(LAST_PIC_FILE):
        with open(LAST_PIC_FILE, "r") as f:
            return f.read().strip()
    return None

def save_last_pic_url(url):
    with open(LAST_PIC_FILE, "w") as f:
        f.write(url)

def normalize_url(url: str) -> str:
    # Strip query & fragment so CDN cache-busters don’t cause false “changes”
    parsed = urlparse(url)
    cleaned = parsed._replace(query="", fragment="")
    return urlunparse(cleaned)

def download_image(url, filename):
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    os.makedirs(PIC_DIR, exist_ok=True)
    path = os.path.join(PIC_DIR, filename)
    with open(path, "wb") as f:
        for chunk in resp.iter_content(8192):
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
    # Best‑effort: handle EU cookie dialog if shown
    texts = [
        "Only allow essential cookies",
        "Allow all cookies",
        "Accept all",
        "Allow essential cookies",
        "Accept"
    ]
    for t in texts:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(., '{t}')]"))
            )
            btn.click()
            time.sleep(0.5)
            return
        except Exception:
            pass

def _get_profile_img_src(driver) -> str | None:
    """
    Try several selectors commonly present in mobile/guest views.
    Fallback to og:image if no <img> is reachable.
    """
    selectors = [
        # Most reliable on mobile profile header:
        "img[alt$='profile picture']",
        "img[alt*='profile picture']",
        # Generic header fallbacks:
        "header img[alt$='profile picture']",
        "header a img[alt$='profile picture']",
        "header a img",
    ]

    # Wait up to ~12s for any of the image selectors to appear
    deadline = time.time() + 12
    while time.time() < deadline:
        for sel in selectors:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                src = elems[0].get_attribute("src")
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
    Prefer the two number buttons in the header on mobile guest profiles.
    If not present (interstitial/layout change), fall back to meta description.
    """
    # Try the two number buttons (“xx followers”, “yy following”)
    try:
        # Give DOM a moment; many builds are slow under CI
        buttons = WebDriverWait(driver, 8).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "header section button"))
        )
        if len(buttons) >= 2:
            followers_text, following_text = buttons[0].text, buttons[1].text
            def num(txt):
                m = re.search(r"[\d,.]+", txt.replace(",", ""))
                return m.group(0) if m else None
            return num(followers_text), num(following_text)
    except Exception:
        pass

    # Fallback: parse from <meta name="description">
    try:
        desc = driver.find_element(By.CSS_SELECTOR, "meta[name='description']").get_attribute("content") or ""
        # Example: "105 followers, 128 following, 6 posts – ..."
        fol = re.search(r"([\d,.]+)\s+followers", desc)
        ing = re.search(r"([\d,.]+)\s+following", desc)
        followers = fol.group(1).replace(",", "") if fol else None
        following = ing.group(1).replace(",", "") if ing else None
        return followers, following
    except Exception:
        return None, None

# --- Main Scraper ---
def scrape_and_log(username: str):
    url = f"https://www.instagram.com/{username}/"

    # 1) Mobile emulation and options
    options = Options()
    options.add_experimental_option("mobileEmulation", {"deviceName": "iPhone X"})
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=390,844")
    options.add_argument("--lang=en-US,en")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )

    # 2) Set Chrome Binary & Driver from environment variables (for GitHub Actions)
    chrome_path = os.environ.get("CHROME_PATH")
    driver_path = os.environ.get("CHROMEDRIVER_PATH")

    if chrome_path:
        options.binary_location = chrome_path
    
    # Fallback for local execution if env vars aren't set
    if not driver_path and ChromeDriverManager:
        print("CHROMEDRIVER_PATH not set, using webdriver-manager.")
        driver_path = ChromeDriverManager().install()

    if not driver_path:
        raise RuntimeError("Could not find chromedriver. Set CHROMEDRIVER_PATH or install it in your PATH.")

    service = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    
    try:
        driver.get(url)

        # Cookie gate if any
        _try_click_cookies(driver)

        # ► Profile picture URL (robust)
        current_pic_url_raw = _get_profile_img_src(driver)
        if not current_pic_url_raw:
            raise RuntimeError("Could not locate profile picture on the page")

        current_pic_url = normalize_url(current_pic_url_raw)
        last_pic_url = load_last_pic_url()

        if current_pic_url != last_pic_url:
            # New picture: download with timestamped name
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{ts}_{username}_profile.jpg"
            download_image(current_pic_url_raw, filename)
            save_last_pic_url(current_pic_url)
            is_updated = 1
        else:
            is_updated = 0

        # ► Followers & Following (robust with fallback)
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

    except Exception as e:
        print(f"An error occurred: {e}")
        driver.save_screenshot("error_screenshot.png") # Save screenshot on error
        raise # Re-raise the exception to ensure the workflow fails correctly
    finally:
        driver.quit()

if __name__ == "__main__":
    print(scrape_and_log("zlamp_a"))
