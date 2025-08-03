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
        # Added 'posts' to the fieldnames
        writer = csv.DictWriter(csvfile, fieldnames=[
            "timestamp", "username", "posts", "followers", "following", "is_picture_updated"
        ])
        if is_new:
            writer.writeheader()
        writer.writerow(entry)

def _get_profile_img_src(driver) -> str | None:
    """
    Try several selectors for the profile picture. Fallback to og:image.
    """
    selectors = [
        "header img",  # Primary selector for desktop profile picture
        "img[alt*='profile picture']",
    ]
    deadline = time.time() + 10
    while time.time() < deadline:
        for sel in selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, sel)
                src = elem.get_attribute("src")
                if src and "profile_pic" in src:
                    return src
            except Exception:
                continue
        time.sleep(0.4)
    # Fallback to meta tag, which is very reliable
    try:
        og = driver.find_element(By.CSS_SELECTOR, "meta[property='og:image']")
        return og.get_attribute("content")
    except Exception:
        pass
    return None

def _get_profile_stats(driver) -> tuple[str | None, str | None, str | None]:
    """
    NEW: Attempts to get Posts, Followers, and Following counts from the page HTML first.
    Falls back to parsing the meta description tag.
    """
    posts, followers, following = None, None, None
    
    # Primary method: Scrape the visible stats list in the header
    try:
        # This selector targets the list of stats in the header on desktop view
        stat_elements = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "header section > ul > li"))
        )

        for item in stat_elements:
            text = item.text.lower()
            # The count is usually in a nested <span> but getting the parent text is safer
            count_text = item.find_element(By.CSS_SELECTOR, 'span, button').text
            # Clean the count, removing commas
            count = count_text.split()[0].replace(",", "")

            if "posts" in text:
                posts = count
            elif "followers" in text:
                followers = count
            elif "following" in text:
                following = count
        
        if any([posts, followers, following]):
            return posts, followers, following

    except Exception:
        pass # If HTML parsing fails, we'll proceed to the fallback method below

    # Fallback method: Parse from <meta name="description">
    try:
        desc = driver.find_element(By.CSS_SELECTOR, "meta[name='description']").get_attribute("content") or ""
        
        posts_re = re.search(r"([\d.,\w]+)\s+Posts", desc, re.IGNORECASE)
        followers_re = re.search(r"([\d.,\w]+)\s+Followers", desc, re.IGNORECASE)
        following_re = re.search(r"([\d.,\w]+)\s+Following", desc, re.IGNORECASE)

        posts = posts_re.group(1).replace(",", "") if posts_re else None
        followers = followers_re.group(1).replace(",", "") if followers_re else None
        following = following_re.group(1).replace(",", "") if following_re else None
        
        return posts, followers, following
    except Exception:
        return None, None, None

# --- Main Scraper ---
def scrape_and_log(username: str):
    profile_url = f"https://www.instagram.com/{username}/"

    options = Options()
    # REMOVED: Mobile Emulation. Switched to desktop mode.
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080") # Standard desktop resolution
    options.add_argument("--lang=en-US,en")
    # UPDATED: Desktop User-Agent
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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

        # Use the new function to get all three stats
        posts, followers, following = _get_profile_stats(driver)

        entry = {
            "timestamp": datetime.now().isoformat(),
            "username": username,
            "posts": posts,
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
