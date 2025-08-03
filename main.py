Of course. I've updated the script to address both points.

The new version will first attempt to fetch the **high-resolution profile picture** using Instagram's data endpoint. If that fails, it will fall back to the on-page image. The downloaded file will also be renamed to the format `TIMESTAMP_profile.jpg`, **removing the username**.

-----

### \#\# `main.py` (Full Updated Code)

Here is the complete, modified `main.py` file. No changes are needed for your workflow file.

```python
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
            "timestamp", "username", "posts", "followers", "following", "is_picture_updated"
        ])
        if is_new:
            writer.writeheader()
        writer.writerow(entry)

def _get_profile_img_src_from_page(driver) -> str | None:
    # Fallback method for scraping the visible (often low-quality) image from the page
    try:
        og = driver.find_element(By.CSS_SELECTOR, "meta[property='og:image']")
        return og.get_attribute("content")
    except Exception:
        pass
    try:
        header_img = driver.find_element(By.CSS_SELECTOR, "header img")
        return header_img.get_attribute("src")
    except Exception:
        return None

def _get_hd_profile_pic_url(username: str, session_id: str | None) -> str | None:
    """Fetches the high-resolution profile picture URL from Instagram's JSON endpoint."""
    if not session_id:
        return None
    
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    headers = {
        # This x-ig-app-id is a public key used by Instagram's web app
        'x-ig-app-id': '936619743392459',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    }
    cookies = { 'sessionid': session_id }
    
    try:
        response = requests.get(url, headers=headers, cookies=cookies, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('data', {}).get('user', {}).get('profile_pic_url_hd')
    except Exception as e:
        print(f"Could not fetch HD profile picture via API: {e}")
        return None

def _get_profile_stats(driver) -> tuple[str | None, str | None, str | None]:
    posts, followers, following = None, None, None
    try:
        stat_elements = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "header section > ul > li"))
        )
        for item in stat_elements:
            text = item.text.lower()
            count_text = item.find_element(By.CSS_SELECTOR, 'span, button').text
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
        pass
    return None, None, None

# --- Main Scraper ---
def scrape_and_log(username: str):
    profile_url = f"https://www.instagram.com/{username}/"

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-US,en")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    chrome_path = os.environ.get("CHROME_PATH")
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chrome_path:
        options.binary_location = chrome_path
    if not driver_path:
        raise RuntimeError("Could not find chromedriver path.")
    service = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    
    try:
        session_id = os.environ.get("INSTAGRAM_SESSION_ID")
        if session_id:
            driver.get("https://www.instagram.com/")
            driver.add_cookie({'name': 'sessionid', 'value': session_id, 'domain': '.instagram.com'})
            print("Successfully added session cookie.")
        
        driver.get(profile_url)

        # Scrape page for stats
        posts, followers, following = _get_profile_stats(driver)
        
        # Scrape on-page picture for change detection
        on_page_pic_url = _get_profile_img_src_from_page(driver)
        if not on_page_pic_url:
             raise RuntimeError("Could not locate profile picture on the page.")

        current_pic_url = normalize_url(on_page_pic_url)
        last_pic_url = load_last_pic_url()
        is_updated = 0

        if current_pic_url != last_pic_url:
            is_updated = 1
            print("New picture detected. Attempting to download high-resolution version.")
            save_last_pic_url(current_pic_url)

            # If changed, try to get the HD URL for the best quality download
            hd_url = _get_hd_profile_pic_url(username, session_id)
            
            # Use HD URL if available, otherwise use the on-page URL as a fallback
            download_url = hd_url if hd_url else on_page_pic_url
            
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            # UPDATED: Filename no longer contains the username
            filename = f"{ts}_profile.jpg"
            
            print(f"Downloading to {filename}...")
            download_image(download_url, filename)

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
```
