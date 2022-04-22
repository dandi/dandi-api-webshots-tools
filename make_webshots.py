#!/usr/bin/env python3
from collections import defaultdict
from dataclasses import dataclass, field
import logging
from multiprocessing import Pipe, Process
from multiprocessing.connection import Connection
from operator import attrgetter
import os
from pathlib import Path
from signal import SIGINT
import socket
import statistics
import time
from typing import Callable, ClassVar, List, Optional, Tuple, Union
from xml.sax.saxutils import escape

import click
from click_loglevel import LogLevel
from dandi.consts import known_instances
from dandi.dandiapi import DandiAPIClient
from psutil import NoSuchProcess
from psutil import Process as PSProcess
from psutil import wait_procs
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import yaml

log = logging.getLogger("make_webshots")

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
        if "\n" in t:
            header = "t=[See below]"
        else:
            header = f"t={escape(t)}"
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
    first = True
    for ls in stats:
        if isinstance(ls.time, str) and "\n" in ls.time:
            if first:
                s += "#### Error Messages\n"
                first = False
            s += f"<pre>{escape(ls.time)}</pre>\n"
    return s


class Webshotter:
    def __init__(self, gui_url: str, headless: bool, login: bool):
        self.gui_url = gui_url
        self.headless = headless
        self.do_login = login
        self.set_driver()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        self.driver.quit()

    def set_driver(self):
        options = Options()
        options.add_argument("--no-sandbox")
        if self.headless:
            options.add_argument("--headless")
        options.add_argument("--incognito")
        # options.add_argument('--disable-gpu')
        options.add_argument("--window-size=1024,1400")
        options.add_argument("--disable-dev-shm-usage")
        # driver.set_page_load_timeout(30)
        # driver.set_script_timeout(30)
        # driver.implicitly_wait(10)
        self.driver = webdriver.Chrome(options=options)
        if self.do_login:
            self.login(os.environ["DANDI_USERNAME"], os.environ["DANDI_PASSWORD"])
        # warm up
        self.driver.get(self.gui_url)

    def login(self, username, password):
        log.info("Logging in ...")
        self.driver.get(self.gui_url)
        self.wait_no_progressbar("v-progress-circular")
        login_button = WebDriverWait(self.driver, 300).until(
            EC.presence_of_element_located((By.XPATH, "//button[@id='login']"))
        )
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
            try:
                self.driver.find_element_by_xpath(
                    '//p[contains(text(), "secondary rate limit")]'
                )
            except NoSuchElementException:
                pass
            else:
                raise RateLimitError("GitHub secondary rate limit exceeded")
            el = WebDriverWait(self.driver, 300).until(
                lambda driver: driver.find_elements(
                    By.XPATH, '//button[@name="authorize"]'
                )
                or self.driver.find_elements_by_class_name("v-avatar")
            )[0]
            if el.tag_name == "button":
                el = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, '//button[@name="authorize"]')
                    )
                )
                el.click()
            else:
                break

    def reset_driver(self):
        try:
            self.driver.quit()  # cleanup if still can
        finally:
            self.set_driver()

    def wait_no_progressbar(self, cls, wait_appear=0):
        if wait_appear:
            # this is a dirty solution to the fact that now progress bar might not
            # even appear for awhile, or at all (e.g. for listing an empty dandiset)
            try:
                log.debug("Wait for progress bar %s to appear", cls)
                t0 = time.time()
                out = WebDriverWait(self.driver, wait_appear, poll_frequency=0.05).until(
                    EC.visibility_of_element_located((By.CLASS_NAME, cls))
                    )
                log.debug(" %s appeared after %fs", cls, time.time() - t0)
            except TimeoutException as e:
                log.debug(" %s failed to appear within %fs, continuing", cls, wait_appear)
                return False  # no need to wait -- it did not come
        log.debug("Wait for progress bar %s to dis-appear", cls)
        out = WebDriverWait(self.driver, 300, poll_frequency=0.1).until(
            EC.invisibility_of_element_located((By.CLASS_NAME, cls))
        )
        return True


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

    def process_dandiset_page(self, ds, urlsuf, page, wait_cls, pbar_cls, act):
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
                    self.driver.get(f"{self.gui_url}/dandiset/{ds}{urlsuf}")
                    log.debug("After get")
                else:
                    log.debug("Before get")
                    self.driver.get(f"{self.gui_url}/dandiset/{ds}")
                    log.debug("After get")
                    log.debug("Before initial wait")
                    self.wait_no_progressbar("v-progress-circular")
                    log.debug("After initial wait")
                if act is not None:
                    log.debug("Before act")
                    act(self.driver)
                    log.debug("After act")
                if wait_cls is not None:
                    log.debug("Wait for %s to appear", wait_cls)
                    WebDriverWait(self.driver, 300, poll_frequency=0.01).until(
                        EC.visibility_of_element_located((By.CLASS_NAME, wait_cls))
                    )
                if pbar_cls is not None:
                    log.debug("Before wait")
                    # TEMP: we will have 3 seconds timeout for empty dandisets.
                    # On Yarik's laptop was taking up to 2 seconds to get pbar to appear.
                    #  Yarik found no way to tell empty dandiset from a "not yet loading" listing
                    self.wait_no_progressbar(pbar_cls, wait_appear=3)
                    log.debug("After wait")
            except TimeoutException:
                log.debug("Timed out")
                return "timeout"
            except WebDriverException:  # as exc:
                # do not bother trying to resurrect - it seems to not working
                # really based on 000040 timeout experience
                raise
                """
                t = str(exc).rstrip()  # so even if we continue out of the loop
                log.warning("Caught %s. Reinitializing", str(exc))
                # it might be a reason for subsequent "Max retries exceeded"
                # since it closes "too much"
                self.reset_driver()
                continue
                """
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


@dataclass
class FlakeyFeeder:
    MAX_TRIES: ClassVar[int] = 5

    target: Callable
    args: tuple
    process: Optional[Process] = field(init=False, default=None)
    pipe: Optional[Connection] = field(init=False, default=None)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        if self.process is not None:
            self.pipe.close()
            log.debug("Closed the pipe")
            time.sleep(1)  # seems to be critical for closing the browser
            if self.process.is_alive():
                log.debug("Terminating subprocess")
                self.process.terminate()
                self.process.join(2)
                if self.process.is_alive():
                    log.debug("Subprocess did not exit in time; killing")
                    self.process.kill()
            self.process.close()
            self.process = None
            self.pipe = None

    def __call__(self, *x):
        for _ in range(self.MAX_TRIES):
            self.ensure()
            self.pipe.send(x)
            try:
                y = self.pipe.recv()
            except EOFError:
                log.warning("Subprocess exited while processing %r; restarting", x)
                log.debug("Waiting for subprocess to terminate ...")
                self.process.join()
                log.debug("Subprocess terminated")
                continue
            if isinstance(y, Fatality):
                raise RuntimeError(
                    f"Subprocess encountered unrecoverable error: {y.msg}"
                )
            return y
        raise RuntimeError("Subprocess failed too many times; giving up")

    def ensure(self):
        if self.process is None:
            log.debug("Starting subprocess")
            self.start()
        elif not self.process.is_alive():
            if self.process.exitcode == -SIGINT:
                raise KeyboardInterrupt("Child process received Cntrl-C")
            log.debug("Subprocess is dead; restarting")
            self.process.close()
            self.start()

    def start(self):
        self.pipe, subpipe = Pipe()
        self.process = Process(
            target=self.target, args=(*self.args, self.pipe, subpipe)
        )
        self.process.start()
        subpipe.close()


@dataclass
class Fatality:
    msg: str


class RateLimitError(Exception):
    pass


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
    "landing": ("", "mdi-folder", None, None),
    "edit-metadata": (None, "mdi-folder", None, click_edit),
    # TODO: remove ?location= after https://github.com/dandi/dandi-archive/issues/1058
    # is fixed
    "view-data": ("/draft/files?location=", None, "v-progress-linear", None),
}


def snapshot_pipe(dandi_instance, gui_url, log_level, headless, login, c1, conn):
    cfg_log(log_level)
    # <https://stackoverflow.com/a/6567318/744178>
    c1.close()
    # To guarantee that we time out if something gets stuck:
    socket.setdefaulttimeout(300)
    if gui_url is None:
        gui_url = known_instances[dandi_instance].gui
    try:
        with Webshotter(gui_url, headless, login) as ws:
            while True:
                try:
                    ds, page = conn.recv()
                except EOFError:
                    break
                urlsuf, wait_cls, pbar_cls, act = PAGES[page]
                # Try to avoid hitting GitHub's secondary rate limit:
                time.sleep(2)
                t = ws.process_dandiset_page(ds, urlsuf, page, wait_cls, pbar_cls, act)
                conn.send(
                    LoadStat(
                        dandiset=ds,
                        page=page,
                        time=t,
                        label="Edit Metadata"
                        if page == "edit-metadata"
                        else "Go to page",
                        url=f"{gui_url}/dandiset/{ds}{urlsuf}"
                        if urlsuf is not None
                        else None,
                    )
                )
    except RateLimitError as e:
        conn.send(Fatality(str(e)))
        raise
    finally:
        cleanup_children()


@click.command()
@click.option("--gui-url", help="Dandi Archive GUI URL")
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
@click.option(
    "--headless/--no-headless",
    default=True,
    help="Run headless or in a visible instance"
)
@click.option(
    "--login/--no-login",
    default=True,
    help="Login or not login to DANDI archive"
)
@click.argument("dandisets", nargs=-1)
def main(dandi_instance, gui_url, dandisets, log_level, headless, login):
    cfg_log(log_level)
    if dandisets:
        doreadme = False
    else:
        dandisets = get_dandisets(dandi_instance)
        doreadme = True

    # with Webshotter(dandi_instance) as ws:
    #     ws.fetch_logs("initial_log")

    allstats = []
    readme = ""
    with FlakeyFeeder(snapshot_pipe, (dandi_instance, gui_url, log_level, headless, login)) as ff:
        for ds in dandisets:
            Path(ds).mkdir(parents=True, exist_ok=True)
            stats = []
            for page in PAGES:
                stats.append(ff(ds, page))
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


def cfg_log(log_level: int) -> None:
    logging.basicConfig(
        format=(
            "%(asctime)s [%(levelname)-8s] %(processName)s[%(process)d]:"
            " %(name)s: %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        level=log_level,
    )


def cleanup_children() -> None:
    procs = PSProcess().children(recursive=True)
    if not procs:
        return
    log.info("Cleaning up %d child processes", len(procs))
    for p in procs:
        try:
            p.terminate()
        except NoSuchProcess:
            pass
    gone, alive = wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except NoSuchProcess:
            pass


if __name__ == "__main__":
    main()
