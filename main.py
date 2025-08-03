import time
import re
import shutil
import requests
import os
import csv
import hashlib
from datetime import datetime
from urllib import parse

# Use the modern zoneinfo if available (Python 3.9+), otherwise fall back to pytz
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from pytz import timezone as ZoneInfo

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

# --- Config ---
LAST_PIC_HASH_FILE = "last_pic_hash.txt"
PIC_DIR       = "profile_pics"
LOG_FILE      = "profile_log.csv"

# --- Helpers ---
def load_last_pic_hash():
    if os.path.exists(LAST_PIC_HASH_FILE):
        with open(LAST_PIC_HASH_FILE, "r") as f:
            return f.read().strip()
    return None

def save_last_pic_hash(h: str):
    with open(LAST_PIC_HASH_FILE, "w") as f:
        f.write(h)

def log_to_csv(entry: dict):
    is_new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            "timestamp", "posts", "followers", "following", "is_picture_updated"
        ])
        if is_new:
            writer.writeheader()
        writer.writerow(entry)

def _get_profile_img_src_from_page(driver) -> str | None:
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

def _get_biggest_profile_pic_url(username: str, session_id: str | None) -> str | None:
    if not session_id:
        return None
    headers = {
        'x-ig-app-id': '936619743392459',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    }
    cookies = {'sessionid': session_id}
    try:
        user_info_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
        user_info_resp = requests.get(user_info_url, headers=headers, cookies=cookies, timeout=10)
        user_info_resp.raise_for_status()
        user_id = user_info_resp.json().get('data', {}).get('user', {}).get('id')
        if not user_id: return None
        detail_info_url = f"https://i.instagram.com/api/v1/users/{user_id}/info/"
        detail_info_resp = requests.get(detail_info_url, headers=headers, cookies=cookies, timeout=10)
        detail_info_resp.raise_for_status()
        hd_versions = detail_info_resp.json().get('user', {}).get('hd_profile_pic_versions', [])
        return hd_versions[0].get('url') if hd_versions else detail_info_resp.json().get('user', {}).get('profile_pic_url_hd')
    except Exception as e:
        print(f"Could not fetch biggest profile picture via API: {e}")
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

def scrape_and_log(username: str):
    profile_url = f"https://www.instagram.com/{username}/"
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-US,en")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

    driver = None
    try:
        service = Service(executable_path=os.environ.get("CHROMEDRIVER_PATH"))
        driver = webdriver.Chrome(service=service, options=options)
        
        session_id = os.environ.get("INSTAGRAM_SESSION_ID")
        if session_id:
            driver.get("https://www.instagram.com/")
            driver.add_cookie({'name': 'sessionid', 'value': session_id, 'domain': '.instagram.com'})
            print("Successfully added session cookie.")
        
        driver.get(profile_url)
        time.sleep(3) # Wait for page to load and potentially redirect

        # NEW: Check if we landed on a login page by looking for the password field.
        if driver.find_elements(By.NAME, "password"):
            raise RuntimeError(
                "Authentication failed: Landed on a login page. "
                "Your INSTAGRAM_SESSION_ID cookie is likely expired or invalid. "
                "Please update it in your GitHub Secrets."
            )
        print("Authentication successful, proceeding with scrape.")

        posts, followers, following = _get_profile_stats(driver)
        pic_url_to_check = _get_biggest_profile_pic_url(username, session_id) or _get_profile_img_src_from_page(driver)
        
        if not pic_url_to_check:
            raise RuntimeError("Could not locate profile picture using any method.")

        is_updated = 0
        try:
            response = requests.get(pic_url_to_check, timeout=30)
            response.raise_for_status()
            image_content = response.content
            current_hash = hashlib.md5(image_content).hexdigest()
            last_hash = load_last_pic_hash()
            
            if current_hash != last_hash:
                is_updated = 1
                print("New picture detected (hashes do not match). Saving new image.")
                save_last_pic_hash(current_hash)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{ts}_profile.jpg"
                path = os.path.join(PIC_DIR, filename)
                os.makedirs(PIC_DIR, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(image_content)
                print(f"Saved new image to {path}")

        except requests.exceptions.RequestException as e:
            print(f"Failed to download image for hashing: {e}")
        
        warsaw_tz = ZoneInfo("Europe/Warsaw")
        formatted_timestamp = timestamp_now.strftime("%A, %d %B %Y %H:%M")

        entry = {
            "timestamp": formatted_timestamp,
            "posts": posts,
            "followers": followers,
            "following": following,
            "is_picture_updated": is_updated,
        }
        log_to_csv(entry)
        return entry

    except Exception as e:
        print(f"An error occurred: {e}")
        if driver:
            driver.save_screenshot("error_screenshot.png")
        raise
    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    print(scrape_and_log("zlamp_a"))
