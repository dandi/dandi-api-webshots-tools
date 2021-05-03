#!/bin/bash
set -ex

PYTHON=$HOME/miniconda3/bin/python

cd "$(dirname "$0")"/..
git reset --hard HEAD
git clean -df
git checkout master
git pull

if [ ! -e venv ]
then $PYTHON -m virtualenv venv
     venv/bin/pip install -r tools/requirements.txt
fi

. venv/bin/activate

set +x
. ~/secrets.env
export DANDI_USERNAME=dandibot
export DANDI_PASSWORD="$DANDIBOT_GITHUB_PASSWORD"
set -x

xvfb-run python tools/make_webshots.py

git add .
if ! git diff --quiet --cached
then git commit -m "Automatically update webshots"
     git push
else echo "No changes to commit"
fi

# vim:set et sts=4:
