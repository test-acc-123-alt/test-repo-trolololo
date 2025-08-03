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

def _get_profile_img_src(driver) -> str | None:
    selectors = [
        "img[alt$='profile picture']",
        "img[alt*='profile picture']",
        "header img[alt$='profile picture']",
        "header a img",
    ]
    deadline = time.time() + 12
    while time.time() < deadline:
        for sel in selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, sel)
                src = elem.get_attribute("src")
                if src:
                    return src
            except Exception:
                continue
        time.sleep(0.4)
    try:
        og = driver.find_element(By.CSS_SELECTOR, "meta[property='og:image']")
        return og.get_attribute("content")
    except Exception:
        pass
    return None

def _get_follow_counts(driver) -> tuple[str | None, str | None]:
    try:
        links = WebDriverWait(driver, 8).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[href$='/followers/'], a[href$='/following/']"))
        )
        followers_text = "0"
        following_text = "0"
        for link in links:
            if "followers" in link.get_attribute("href"):
                followers_text = link.text
            elif "following" in link.get_attribute("href"):
                following_text = link.text
        
        def num(txt):
            m = re.search(r"[\d,.]+", txt.replace(",", ""))
            return m.group(0) if m else None
        return num(followers_text), num(following_text)
    except Exception:
        pass
    try:
        desc = driver.find_element(By.CSS_SELECTOR, "meta[name='description']").get_attribute("content") or ""
        fol = re.search(r"([\d,.]+)\s+Followers", desc)
        ing = re.search(r"([\d,.]+)\s+Following", desc)
        followers = fol.group(1).replace(",", "") if fol else None
        following = ing.group(1).replace(",", "") if ing else None
        return followers, following
    except Exception:
        return None, None

# --- Main Scraper ---
def scrape_and_log(username: str):
    profile_url = f"https://www.instagram.com/{username}/"

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

    chrome_path = os.environ.get("CHROME_PATH")
    driver_path = os.environ.get("CHROMEDRIVER_PATH")

    if chrome_path:
        options.binary_location = chrome_path
    
    if not driver_path and ChromeDriverManager:
        driver_path = ChromeDriverManager().install()

    if not driver_path:
        raise RuntimeError("Could not find chromedriver.")

    service = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    
    try:
        # Load session cookie to bypass login wall
        session_id = os.environ.get("INSTAGRAM_SESSION_ID")
        if session_id:
            driver.get("https://www.instagram.com/") 
            driver.add_cookie({
                'name': 'sessionid',
                'value': session_id,
                'domain': '.instagram.com',
            })
            print("Successfully added session cookie.")
        
        driver.get(profile_url) 

        current_pic_url_raw = _get_profile_img_src(driver)
        if not current_pic_url_raw:
            raise RuntimeError("Could not locate profile picture on the page")

        current_pic_url = normalize_url(current_pic_url_raw)
        last_pic_url = load_last_pic_url()
        is_updated = 0
        if current_pic_url != last_pic_url:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{ts}_{username}_profile.jpg"
            download_image(current_pic_url_raw, filename)
            save_last_pic_url(current_pic_url)
            is_updated = 1

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
        driver.save_screenshot("error_screenshot.png")
        raise
    finally:
        driver.quit()

if __name__ == "__main__":
    print(scrape_and_log("zlamp_a"))
