#!/usr/bin/env python3
from functools import partial
import os
from pathlib import Path
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import yaml

ARCHIVE_GUI = "https://gui-beta-dandiarchive-org.netlify.app"


def get_dandisets():
    """Return a list of known dandisets"""
    from dandi.dandiapi import DandiAPIClient
    client = DandiAPIClient('https://api.dandiarchive.org/api')
    dandisets = client.get('/dandisets', parameters={'page_size': 10000})
    return sorted(x['identifier'] for x in dandisets['results'])


def login(driver, username, password):
    driver.get(ARCHIVE_GUI)
    wait_no_progressbar(driver, "v-progress-circular")
    try:
        login_button = driver.find_elements_by_xpath(
            "//*[@id='app']/div/header/div/button[2]"
        )[0]
        login_text = login_button.text.strip().lower()
        assert login_text == "login", f"Login button did not have expected text; expected 'login', got {login_text!r}"
        login_button.click()

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "login_field")))

        username_field = driver.find_element_by_id("login_field")
        password_field = driver.find_element_by_id("password")
        username_field.send_keys(username)
        password_field.send_keys(password)
        driver.find_elements_by_tag_name("form")[0].submit()

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CLASS_NAME, "v-avatar")))
    except Exception:
        driver.save_screenshot("failure.png")
        raise


def wait_no_progressbar(driver, cls):
    WebDriverWait(driver, 30).until(
        EC.invisibility_of_element_located((By.CLASS_NAME, cls)))


def process_dandiset(driver, ds):

    def click_edit():
        submit_button = driver.find_elements_by_xpath(
            '//*[@id="app"]/div/main/div/div/div/div/div[1]/div/div[2]/div[1]/div[3]/button[1]'
            )[0]
        submit_button.click()

    dspath = Path(ds)
    if not dspath.exists():
        dspath.mkdir(parents=True)

    info = {'times': {}}
    times = info['times']


    # TODO: do not do draft unless there is one
    # TODO: do for a released version
    for urlsuf, page, wait, act in [
        ('', 'landing', partial(wait_no_progressbar, driver, "v-progress-circular"), None),
        # without login I cannot edit metadata, so let it not be used for now
        # (None, 'edit-metadata', None, click_edit),
        ('/draft/files', 'view-data', partial(wait_no_progressbar, driver, "v-progress-linear"), None)]:

        page_name = dspath / page

        t0 = time.monotonic()
        if urlsuf is not None:
            driver.get(f'{ARCHIVE_GUI}/#/dandiset/{ds}{urlsuf}')
        if act:
            act()
        if wait:
            wait()
        times[page] = time.monotonic() - t0
        page_name.with_suffix('.html').write_text(driver.page_source)
        driver.save_screenshot(str(page_name.with_suffix('.png')))


    with (dspath / 'info.yaml').open('w') as f:
        yaml.safe_dump(info, f)

    # quick and dirty for now, although should just come from the above "structure"
    return f"""
### {ds}

| t={times['landing']:.2f} [Go to page]({ARCHIVE_GUI}/#/dandiset/{ds}) | t={times['view-data']:.2f} [Go to page]({ARCHIVE_GUI}/#/dandiset/{ds}/draft/files) |
| --- | --- |
| ![]({ds}/landing.png) | ![]({ds}/view-data.png) |

"""


if __name__ == '__main__':
    if len(sys.argv) > 1:
        dandisets = sys.argv[1:]
        doreadme = False
    else:
        dandisets = get_dandisets()
        doreadme = True

    readme = ''
    driver = webdriver.Chrome()
    # warm up
    driver.get(ARCHIVE_GUI)
    login(driver, os.environ["DANDI_USERNAME"], os.environ["DANDI_PASSWORD"])
    for ds in dandisets:
        readme += process_dandiset(driver, ds)
    driver.quit()

    if doreadme:
        Path('README.md').write_text(readme)
