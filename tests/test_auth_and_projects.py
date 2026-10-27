from uuid import uuid4

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as expected
from selenium.webdriver.support.ui import WebDriverWait


SELECTORS = {
    "login_username": (By.ID, "l-u"),
    "login_password": (By.ID, "l-p"),
    "login_form": (By.ID, "f-login"),
    "login_submit": (By.CSS_SELECTOR, "#f-login button[type='submit']"),
    "projects_page": (By.CSS_SELECTOR, ".topbar"),
    "new_project": (By.CSS_SELECTOR, ".toolbar button.btn.primary"),
    "project_dialog": (By.CSS_SELECTOR, "#dc[open]"),
    "project_name": (By.CSS_SELECTOR, "#dc input[name='name']"),
    "project_description": (By.CSS_SELECTOR, "#dc textarea[name='description']"),
    "project_create": (By.CSS_SELECTOR, "#dc button.btn.primary"),
    "members_tab": (By.CSS_SELECTOR, "[data-tab='members']"),
    "danger_zone": (By.XPATH, "//summary[contains(., 'Danger zone')]"),
    "delete_project": (By.XPATH, "//button[contains(., 'Delete project')]"),
}


def wait_for(driver, timeout=10):
    return WebDriverWait(driver, timeout)


def sign_in(driver, base_url, username, password):
    driver.get(f"{base_url}/accounts/?next=/projects/")
    wait = wait_for(driver)

    login_form = wait.until(expected.visibility_of_element_located(SELECTORS["login_form"]))
    username_field = login_form.find_element(*SELECTORS["login_username"])
    password_field = login_form.find_element(*SELECTORS["login_password"])
    assert username_field.is_displayed()
    assert password_field.is_displayed()

    username_field.send_keys(username)
    password_field.send_keys(password)
    assert username_field.get_attribute("value") == username
    assert password_field.get_attribute("value") == password

    wait.until(expected.element_to_be_clickable(SELECTORS["login_submit"])).click()
    wait.until(expected.url_contains("/projects/"))
    wait.until(expected.visibility_of_element_located(SELECTORS["projects_page"]))


def delete_current_project(driver):
    wait = wait_for(driver)
    wait.until(expected.element_to_be_clickable(SELECTORS["members_tab"])).click()
    wait.until(expected.element_to_be_clickable(SELECTORS["danger_zone"])).click()
    delete_button = wait.until(expected.element_to_be_clickable(SELECTORS["delete_project"]))
    assert delete_button.is_displayed()
    delete_button.click()
    wait.until(expected.alert_is_present()).accept()
    wait.until(expected.url_contains("/projects/"))


def test_user_can_log_in_with_verified_credentials(driver, base_url, verified_user):
    """TC-01: a previously verified user can sign in with valid credentials."""
    username, password = verified_user

    sign_in(driver, base_url, username, password)

    assert driver.current_url.rstrip("/").endswith("/projects")
    assert driver.find_element(*SELECTORS["projects_page"]).is_displayed()


def test_user_can_create_and_delete_project(driver, base_url, verified_user):
    """TC-02: an owner can create and delete a project."""
    username, password = verified_user
    project_name = f"Selenium project {uuid4().hex[:8]}"
    project_description = "Created by Selenium."
    sign_in(driver, base_url, username, password)
    wait = wait_for(driver)

    wait.until(expected.element_to_be_clickable(SELECTORS["new_project"])).click()
    dialog = wait.until(expected.visibility_of_element_located(SELECTORS["project_dialog"]))
    name_field = dialog.find_element(*SELECTORS["project_name"])
    description_field = dialog.find_element(*SELECTORS["project_description"])
    name_field.send_keys(project_name)
    description_field.send_keys(project_description)
    assert name_field.get_attribute("value") == project_name
    assert description_field.get_attribute("value") == project_description

    dialog.find_element(*SELECTORS["project_create"]).click()
    wait.until(expected.url_matches(r".*/p/\d+/$"))
    assert project_name in driver.page_source

    try:
        wait.until(expected.element_to_be_clickable(SELECTORS["members_tab"])).click()
        wait.until(expected.element_to_be_clickable(SELECTORS["danger_zone"])).click()
        assert wait.until(expected.visibility_of_element_located(SELECTORS["delete_project"])).is_displayed()
        delete_current_project(driver)
    finally:
        if "/p/" in driver.current_url:
            driver.get(f"{base_url}/projects/")

    assert project_name not in driver.page_source
