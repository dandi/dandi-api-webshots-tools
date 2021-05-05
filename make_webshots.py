#!/usr/bin/env python3
from functools import partial
import logging
import os
from pathlib import Path
import socket
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common.exceptions import TimeoutException
import yaml

log = logging.getLogger(__name__)

ARCHIVE_GUI = "https://gui.dandiarchive.org"


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
        assert login_text == "login or create account", f"Login button did not have expected text; expected 'login', got {login_text!r}"
        login_button.click()

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "login_field")))

        username_field = driver.find_element_by_id("login_field")
        password_field = driver.find_element_by_id("password")
        username_field.send_keys(username)
        password_field.send_keys(password)
        #driver.save_screenshot("logging-in.png")
        driver.find_elements_by_tag_name("form")[0].submit()

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CLASS_NAME, "v-avatar")))
    except Exception:
        #driver.save_screenshot("failure.png")
        raise


def wait_no_progressbar(driver, cls):
    WebDriverWait(driver, 30).until(
        EC.invisibility_of_element_located((By.CLASS_NAME, cls)))


def process_dandiset(driver, ds):

    def click_edit():
        # might still take a bit to appear
        # TODO: more sensible way to "identify" it: https://github.com/dandi/dandiarchive/issues/648
        edit_button = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 '//*[@id="app"]/div/main/div/div/div/div/div[1]/div/div[2]/div[1]/div[3]/button[1]')))
        edit_button.click()

    dspath = Path(ds)
    if not dspath.exists():
        dspath.mkdir(parents=True)

    info = {'times': {}}
    times = info['times']

    # TODO: do not do draft unless there is one
    # TODO: do for a released version
    for urlsuf, page, wait, act in [
        ('', 'landing', partial(wait_no_progressbar, driver, "v-progress-circular"), None),
        (None, 'edit-metadata', partial(wait_no_progressbar, driver, "v-progress-circular"), click_edit),
        ('/draft/files', 'view-data', partial(wait_no_progressbar, driver, "v-progress-linear"), None)]:

        log.info(f"{ds} {page}")
        page_name = dspath / page
        # so if we fail, we do not carry outdated one
        page_name.with_suffix('.png').unlink(missing_ok=True)
        t0 = time.monotonic()
        try:
            if urlsuf is not None:
                log.debug("Before get")
                driver.get(f'{ARCHIVE_GUI}/#/dandiset/{ds}{urlsuf}')
                log.debug("After get")
            if act:
                log.debug("Before act")
                act()
                log.debug("After act")
            if wait:
                log.debug("Before wait")
                wait()
                log.debug("After wait")
        except TimeoutException:
            times[page] = 'timeout'
        except Exception as exc:
            times[page] = str(exc)
        else:
            times[page] = time.monotonic() - t0
            time.sleep(2)  # to overcome https://github.com/dandi/dandiarchive/issues/650 - animations etc
            driver.save_screenshot(str(page_name.with_suffix('.png')))
        # now that we do login, do not bother storing html to not leak anything sensitive by mistake
        # page_name.with_suffix('.html').write_text(driver.page_source)

    with (dspath / 'info.yaml').open('w') as f:
        yaml.safe_dump(info, f)

    times_ = {
        k: (v if isinstance(v, str) else '%.2f' % v)
        for k, v in times.items()
    }
    # quick and dirty for now, although should just come from the above "structure"
    return f"""
### {ds}

| t={times_['landing']} [Go to page]({ARCHIVE_GUI}/#/dandiset/{ds}) | t={times_['edit-metadata']} Edit Metadata | t={times_['view-data']} [Go to page]({ARCHIVE_GUI}/#/dandiset/{ds}/draft/files) |
| --- | --- | --- |
| ![]({ds}/landing.png) | ![]({ds}/edit-metadata.png) | ![]({ds}/view-data.png) |

"""


if __name__ == '__main__':
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        level=logging.ERROR,
    )

    if len(sys.argv) > 1:
        dandisets = sys.argv[1:]
        doreadme = False
    else:
        dandisets = get_dandisets()
        doreadme = True

    readme = ''
    options = Options()
    options.add_argument('--no-sandbox')
    options.add_argument('--headless')
    options.add_argument('--incognito')
    #options.add_argument('--disable-gpu')
    options.add_argument("--window-size=1024,768")
    options.add_argument('--disable-dev-shm-usage')
    driver = webdriver.Chrome(options=options)
    #driver.set_page_load_timeout(30)
    #driver.set_script_timeout(30)
    #driver.implicitly_wait(10)
    # To guarantee that we time out if something gets stuck
    socket.setdefaulttimeout(30)
    # warm up
    driver.get(ARCHIVE_GUI)
    login(driver, os.environ["DANDI_USERNAME"], os.environ["DANDI_PASSWORD"])
    for ds in dandisets:
        readme += process_dandiset(driver, ds)
    driver.quit()

    if doreadme:
        Path('README.md').write_text(readme)
