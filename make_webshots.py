#!/usr/bin/env python3

import sys
import time

import yaml

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from selenium import webdriver

from pathlib import Path


def get_dandisets():
    """Return a list of known dandisets"""
    from dandi.dandiapi import DandiAPIClient


def process_dandiset(ds):
    driver = webdriver.Chrome()

    def wait_no_progressbar():
        WebDriverWait(driver, 30).until(
            EC.invisibility_of_element_located((By.CLASS_NAME, "v-progress-circular")))

    ds = '000003'
    dspath = Path(ds)
    if not dspath.exists():
        dspath.mkdir(parents=True)

    info = {'times': {}}
    times = info['times']


    # TODO: do not do draft unless there is one
    # TODO: do for a released version
    for urlsuf, page, wait in [
        ('', 'landing', wait_no_progressbar),
        ('/draft/files', 'view-data', None)]:

        page_name = dspath / page

        t0 = time.monotonic()
        driver.get(f'https://gui-beta-dandiarchive-org.netlify.app/#/dandiset/{ds}{urlsuf}')
        if wait:
            wait()
        times[page] = time.monotonic() - t0
        page_name.with_suffix('.html').write_text(driver.page_source)
        driver.save_screenshot(str(page_name.with_suffix('.png')))

    with (dspath / 'info.yaml').open('w') as f:
        yaml.safe_dump(info, f)

    driver.quit()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        dandisets = sys.argv[1:]
    else:
        dandisets = get_dandisets()
    for ds in dandisets:
        process_dandiset(ds)

