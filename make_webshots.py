#!/usr/bin/env python3
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import partial
import itertools
import logging
from operator import attrgetter
import os
from pathlib import Path
import socket
import statistics
import time
from typing import List, Optional, Tuple, Union

import click
from click_loglevel import LogLevel
from dandi.consts import known_instances
from dandi.dandiapi import DandiAPIClient
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import yaml

log = logging.getLogger(__name__)

# set to True to fetch the logs, not enabled by default
FETCH_CONSOLE_LOGS = False


@dataclass
class LoadStat:
    dandiset: str
    page: str
    time: Union[float, str]
    label: str
    url: Optional[str]

    def get_columns(self) -> Tuple[str, str]:
        t = self.time if isinstance(self.time, str) else f"{self.time:.2f}"
        header = f"t={t}"
        if self.url is not None:
            header += f" [{self.label}]({self.url})"
        else:
            header += f" {self.label}"
        cell = f"![]({self.dandiset}/{self.page}.png)"
        return (header, cell)

    def has_time(self) -> bool:
        return isinstance(self.time, float)


def render_stats(dandiset: str, stats: List[LoadStat]) -> str:
    s = f"### {dandiset}\n\n"
    header, row = zip(*map(LoadStat.get_columns, stats))
    s += "| " + " | ".join(header) + " |\n"
    s += "| --- " * len(stats) + "|\n"
    s += "| " + " | ".join(row) + " |\n"
    s += "\n"
    return s


class Webshotter:
    def __init__(self, gui_url: str):
        self.gui_url = gui_url
        self.set_driver()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.driver.quit()

    def set_driver(self):
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--headless")
        options.add_argument("--incognito")
        # options.add_argument('--disable-gpu')
        options.add_argument("--window-size=1024,1400")
        options.add_argument("--disable-dev-shm-usage")
        # driver.set_page_load_timeout(30)
        # driver.set_script_timeout(30)
        # driver.implicitly_wait(10)
        self.driver = webdriver.Chrome(options=options)
        self.login(os.environ["DANDI_USERNAME"], os.environ["DANDI_PASSWORD"])
        # warm up
        self.driver.get(self.gui_url)

    def login(self, username, password):
        self.driver.get(self.gui_url)
        self.wait_no_progressbar("v-progress-circular")
        login_button = self.driver.find_elements_by_xpath("//button[@id='login']")[0]
        login_text = login_button.text.strip().lower()
        assert "log in" in login_text.lower(), (
            "Login button did not have expected text; expected 'log in',"
            f" got {login_text!r}"
        )
        login_button.click()
        WebDriverWait(self.driver, 300).until(
            EC.presence_of_element_located((By.ID, "login_field"))
        )
        username_field = self.driver.find_element_by_id("login_field")
        password_field = self.driver.find_element_by_id("password")
        username_field.send_keys(username)
        password_field.send_keys(password)
        # self.driver.save_screenshot("logging-in.png")
        self.driver.find_elements_by_tag_name("form")[0].submit()

        # Here we might get "Authorize" dialog or not
        # Solution based on https://stackoverflow.com/a/61895999/1265472
        # chose as the most straight-forward
        for _ in range(2):
            el = WebDriverWait(self.driver, 300).until(
                lambda driver: driver.find_elements(
                    By.XPATH, '//input[@value="Authorize"]'
                )
                or self.driver.find_elements_by_class_name("v-avatar")
            )[0]
            if el.tag_name == "input":
                el.click()
            else:
                break

    def reset_driver(self):
        try:
            self.driver.quit()  # cleanup if still can
        finally:
            self.set_driver()

    def wait_no_progressbar(self, cls):
        WebDriverWait(self.driver, 300, poll_frequency=0.1).until(
            EC.invisibility_of_element_located((By.CLASS_NAME, cls))
        )

    def fetch_logs(self, filename=None):
        """
        Given current state of the browser logs, fetch them and (if filename
        provided) save to a file

        Only new logs are fetch in subsequent invocation, so just use
        fetch_logs to swallow all you do not care about.

        `filename` can have some other extension, will be replaced with .yaml

        Logs are dumped only if any.  file (under filename) is removed first
        regardless.
        """
        if not FETCH_CONSOLE_LOGS:
            return
        logs = self.driver.get_log("browser")
        if filename:
            fileobj = Path(filename).with_suffix(".yaml")
            fileobj.unlink(missing_ok=True)
            if logs:
                with fileobj.open("w") as f:
                    yaml.safe_dump(logs, f)
        return logs

    def process_dandiset_page(self, ds, urlsuf, page, wait_cls, act):
        # TODO: do not do draft unless there is one
        # TODO: do for a released version
        log.info("%s %s", ds, page)
        page_name = Path(ds, page)
        # So we could try a few times in case of catching WebDriverException
        # e.g. as in the case of "invalid session id" whenever we would
        # reinitialize the entire driver
        for _ in range(3):
            page_name.with_suffix(".png").unlink(missing_ok=True)
            t0 = time.monotonic()
            # ad-hoc workaround for https://github.com/dandi/dandiarchive/issues/662
            # with hope it is the only one and to not overcomplicate things so
            # if we fail, we do not carry outdated one
            # if ds in ('000040', '000041') and page == 'edit-metadata':
            #    t = "timeout/crash"
            #    break
            try:
                if urlsuf is not None:
                    log.debug("Before get")
                    self.driver.get(f"{self.gui_url}/#/dandiset/{ds}{urlsuf}")
                    log.debug("After get")
                else:
                    log.debug("Before get")
                    self.driver.get(f"{self.gui_url}/#/dandiset/{ds}")
                    log.debug("After get")
                    log.debug("Before initial wait")
                    self.wait_no_progressbar("v-progress-circular")
                    log.debug("After initial wait")
                if act is not None:
                    log.debug("Before act")
                    act(self.driver)
                    log.debug("After act")
                if wait_cls is not None:
                    log.debug("Before wait")
                    self.wait_no_progressbar(wait_cls)
                    log.debug("After wait")
            except TimeoutException:
                log.debug("Timed out")
                return "timeout"
            except WebDriverException as exc:
                # do not bother trying to resurrect - it seems to not working
                # really based on 000040 timeout experience
                raise
                t = str(exc).rstrip()  # so even if we continue out of the loop
                log.warning("Caught %s. Reinitializing", str(exc))
                # it might be a reason for subsequent "Max retries exceeded"
                # since it closes "too much"
                self.reset_driver()
                continue
            except Exception as exc:
                log.warning("Caught unexpected %s.", str(exc))
                return str(exc).rstrip()
            else:
                t = time.monotonic() - t0
                # to overcome https://github.com/dandi/dandiarchive/issues/650
                # - animations etc:
                time.sleep(2)
                self.driver.save_screenshot(str(page_name.with_suffix(".png")))
                self.fetch_logs(page_name)
                return t


def get_dandisets(dandi_instance):
    """Return a list of known Dandiset IDs"""
    with DandiAPIClient.for_dandi_instance(dandi_instance) as client:
        for d in sorted(client.get_dandisets(), key=attrgetter("identifier")):
            yield d.identifier


def click_edit(driver):
    # might still take a bit to appear
    # TODO: more sensible way to "identify" it:
    # https://github.com/dandi/dandiarchive/issues/648
    edit_button = WebDriverWait(driver, 3).until(
        EC.element_to_be_clickable((By.XPATH, '//button[@id="view-edit-metadata"]'))
    )
    edit_button.click()


PAGES = {
    "landing": ("", "v-progress-circular", None),
    "edit-metadata": (None, "v-progress-circular", click_edit),
    "view-data": ("/draft/files", "v-progress-linear", None),
}


def snapshot_page(dandi_instance, log_level, ds_page):
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(process)d %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        level=log_level,
    )
    # To guarantee that we time out if something gets stuck:
    socket.setdefaulttimeout(300)
    gui_url = known_instances[dandi_instance].gui
    ds, page = ds_page
    urlsuf, wait_cls, act = PAGES[page]
    try:
        with Webshotter(gui_url) as ws:
            t = ws.process_dandiset_page(ds, urlsuf, page, wait_cls, act)
    except TimeoutException:
        # This can happen if a timeout occurs inside the Webshotter constructor
        # (e.g., when trying to log in)
        log.debug("Startup timed out")
        t = "timeout"
    except WebDriverException as exc:
        log.warning("Caught %s", str(exc))
        t = str(exc).rstrip()
    return LoadStat(
        dandiset=ds,
        page=page,
        time=t,
        label="Edit Metadata" if page == "edit-metadata" else "Go to page",
        url=f"{gui_url}/#/dandiset/{ds}{urlsuf}" if urlsuf is not None else None,
    )


@click.command()
@click.option(
    "-i",
    "--dandi-instance",
    help="DANDI instance to use",
    type=click.Choice(sorted(known_instances)),
    default="dandi",
    show_default=True,
)
@click.option(
    "-l",
    "--log-level",
    type=LogLevel(),
    default=logging.INFO,
    help="Set logging level  [default: INFO]",
)
@click.argument("dandisets", nargs=-1)
def main(dandi_instance, dandisets, log_level):
    if dandisets:
        doreadme = False
    else:
        dandisets = list(get_dandisets(dandi_instance))
        doreadme = True
    for ds in dandisets:
        Path(ds).mkdir(parents=True, exist_ok=True)

    # with Webshotter(dandi_instance) as ws:
    #     ws.fetch_logs("initial_log")

    statdict = defaultdict(dict)
    with ProcessPoolExecutor(max_workers=1) as executor:
        for stat in executor.map(
            partial(snapshot_page, dandi_instance, log_level),
            itertools.product(dandisets, PAGES.keys()),
        ):
            statdict[stat.dandiset][stat.page] = stat

    allstats = []
    readme = ""
    for ds, raw_stats in sorted(statdict.items()):
        stats = [raw_stats[p] for p in PAGES.keys()]
        times = {st.page: st.time for st in stats}
        with Path(ds, "info.yaml").open("w") as f:
            yaml.safe_dump({"times": times}, f)
        readme += render_stats(ds, stats)
        allstats.extend(stats)

    if doreadme:
        stat_tbl = "| Page | Min Time | Mean ± StdDev | Max Time | Errors |\n"
        stat_tbl += "| --- | --- | --- | --- | --- |\n"
        page_stats = defaultdict(list)
        errors = defaultdict(list)
        for st in allstats:
            if st.has_time():
                page_stats[st.page].append(st)
            else:
                errors[st.page].append(st.dandiset)
        for page in PAGES.keys():
            stats = page_stats[page]
            if stats:
                minstat = min(stats, key=attrgetter("time"))
                min_cell = (
                    f"{minstat.time:.2f}s ([{minstat.dandiset}](#{minstat.dandiset}))"
                )
                times = [st.time for st in stats]
                mean = statistics.mean(times)
                stddev = statistics.pstdev(times, mu=mean)
                mean_stddev = f"{mean:.2f}s ± {stddev:.2f}s"
                maxstat = max(stats, key=attrgetter("time"))
                max_cell = (
                    f"{maxstat.time:.2f}s ([{maxstat.dandiset}](#{maxstat.dandiset}))"
                )
            else:
                min_cell = mean_stddev = max_cell = "\u2014"
            if errors[page]:
                errs = ", ".join(f"[{ds}](#{ds})" for ds in errors[page])
            else:
                errs = "\u2014"
            stat_tbl += (
                f"| {page} | {min_cell} | {mean_stddev} | {max_cell} | {errs} |\n"
            )
        readme = stat_tbl + "\n\n" + readme
        Path("README.md").write_text(readme)


if __name__ == "__main__":
    main()
