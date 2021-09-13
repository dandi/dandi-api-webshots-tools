#!/usr/bin/env python3
import os
import subprocess
import click
import requests


@click.command()
@click.option("-R", "--repository", metavar="OWNER/NAME", required=True)
@click.option("--pr", type=int, required=True)
@click.option("--context")
@click.option("-d", "--description")
@click.option(
    "--state",
    type=click.Choice(["error", "failure", "pending", "success"]),
    required=True,
)
@click.option("--target-url")
def main(repository, pr, **kwargs):
    token = get_github_token()
    with requests.Session() as s:
        s.headers["Authorization"] = f"bearer {token}"
        s.headers["Accept"] = "application/vnd.github.v3+json"
        r = s.get(f"https://api.github.com/repos/{repository}/pulls/{pr}")
        r.raise_for_status()
        r.post(r.json()["statuses_url"], json=kwargs).raise_for_status()


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        r = subprocess.run(
            ["git", "config", "hub.oauthtoken"],
            stdout=subprocess.PIPE,
            universal_newlines=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(
                "GitHub OAuth token not set.  Set via GITHUB_TOKEN"
                " environment variable or hub.oauthtoken Git config option."
            )
        token = r.stdout.strip()
    return token


if __name__ == "__main__":
    main()
