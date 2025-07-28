from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time

def scrape_instagram_profile_live(username):
    url = f"https://www.instagram.com/{username}/"

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        driver.get(url)
        time.sleep(5)

        # The stats are inside <ul> in header, with <li> for posts, followers, following
        stats_list = driver.find_elements(By.CSS_SELECTOR, "header ul li")

        if len(stats_list) < 3:
            raise Exception("Could not find profile statistics")

        # Extract text from each <li>
        posts_text = stats_list[0].text  # e.g. "803 posts"
        followers_text = stats_list[1].text  # e.g. "34 followers"
        following_text = stats_list[2].text  # e.g. "124 following"

        # Extract just numbers (handles commas, dots)
        import re
        def extract_number(text):
            match = re.search(r"[\d,.]+", text.replace(",", ""))
            return match.group(0) if match else text

        posts = extract_number(posts_text)
        followers = extract_number(followers_text)
        following = extract_number(following_text)

        return {
            "username": username,
            "posts": posts,
            "followers": followers,
            "following": following
        }

    finally:
        driver.quit()

if __name__ == "__main__":
    profile = scrape_instagram_profile_live("zlamp_a")
    print(profile)
