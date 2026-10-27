import os

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


@pytest.fixture
def driver():
    options = Options()
    if os.getenv("SELENIUM_HEADLESS", "1") == "1":
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    browser = webdriver.Chrome(options=options)
    yield browser
    browser.quit()


@pytest.fixture
def base_url():
    return os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture
def verified_user():
    username = os.getenv("TEST_USERNAME")
    password = os.getenv("TEST_PASSWORD")
    if not username or not password:
        pytest.skip("Set TEST_USERNAME and TEST_PASSWORD for Selenium tests.")
    return username, password
