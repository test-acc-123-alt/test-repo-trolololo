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
from webdriver_manager.chrome import ChromeDriverManager

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


def normalize_url(url):
    # Strip query parameters and fragments
    parsed = urlparse(url)
    cleaned = parsed._replace(query="", fragment="")
    return urlunparse(cleaned)


def download_image(url, filename):
    resp = requests.get(url, stream=True)
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

# --- Main Scraper ---
def scrape_and_log(username: str):
    url = f"https://www.instagram.com/{username}/"

    # 1) Mobile emulation: iPhone X
    mobile_emulation = {"deviceName": "iPhone X"}
    options = Options()
    options.add_experimental_option("mobileEmulation", mobile_emulation)
    # Use new headless mode and disable sandboxing
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--remote-debugging-port=9222")

    # 2) Auto-detect Chrome/Chromium binary
    for name in ("chromium-browser", "chromium", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            options.binary_location = path
            break

    # 3) Launch driver
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.get(url)
        time.sleep(5)  # let everything load

        # ► Profile picture comparison
        img = driver.find_element(By.CSS_SELECTOR, "header a img")
        current_pic_url_raw = img.get_attribute("src")
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

        # ► Followers & Following
        buttons = driver.find_elements(By.CSS_SELECTOR, "header section button")
        if len(buttons) < 2:
            raise RuntimeError("Could not find followers/following buttons")
        followers_text, following_text = buttons[0].text, buttons[1].text

        def extract_num(txt):
            m = re.search(r"[\d,\.]+", txt.replace(",", ""))
            return m.group(0) if m else txt

        # Prepare and write log entry
        entry = {
            "timestamp": datetime.now().isoformat(),
            "username": username,
            "followers": extract_num(followers_text),
            "following": extract_num(following_text),
            "is_picture_updated": is_updated
        }
        log_to_csv(entry)

        return entry

    finally:
        driver.quit()

if __name__ == "__main__":
    result = scrape_and_log("zlamp_a")
    print(result)
