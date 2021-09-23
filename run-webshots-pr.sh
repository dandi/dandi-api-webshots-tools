#!/bin/bash
set -ex

CODE_REPO=dandi/dandiarchive
WEBSHOTS_REPO=dandi/dandi-api-webshots-prs

PYTHON=$HOME/miniconda3/bin/python

cd "$(dirname "$0")"/..
git reset --hard HEAD
git clean -df
git checkout master
git pull

( cd tools; git checkout master; git pull )

if [ ! -e venv ]
then $PYTHON -m virtualenv venv
fi
venv/bin/pip install -r tools/requirements.txt

. venv/bin/activate

set +x
GITHUB_TOKEN="$(git config hub.oauthtoken)"
. ~/secrets.env
export DANDI_USERNAME=dandibot
export DANDI_PASSWORD="$DANDIBOT_GITHUB_PASSWORD"
set -x

for pr
do
    git checkout "pr-$pr" || git checkout -b "pr-$pr"
    pr_head="$(set -o pipefail; curl -fsSL "https://api.github.com/repos/$CODE_REPO/pulls/$pr" | jq -r .head.sha)"
    if [ ! -e pr-head.txt ] || [ "$pr_head" != "$(< pr-head.txt)" ]
    then
        echo "$pr_head" > pr-head.txt

        xvfb-run python tools/make_webshots.py -i dandi-staging \
            --gui-url https://deploy-preview-"$pr"--gui-dandiarchive-org.netlify.app

        errors="$(grep -chP ': [^0-9]' -- */info.yaml | jq -s add)"
        if [ "$errors" -eq 0 ]
        then state=success
        else state=failure
        fi

        jq -n \
            --arg state "$state" \
            --arg target_url "https://github.com/$WEBSHOTS_REPO/tree/pr-$pr" \
            '{
                "state": $state,
                "context": "Webshots",
                "target_url": $target_url
            }' \
        | curl -fsSL -X POST -d @- \
            -H "Authorization: bearer $GITHUB_TOKEN" \
            -H "Content-Type: application/json" \
            "https://api.github.com/repos/$CODE_REPO/statuses/$pr_head"

        git add .
        if ! git diff --quiet --cached
        then git commit -m "Automatically update webshots"
             git push -u origin "pr-$pr"
        else echo "No changes to commit"
        fi
    else echo "No change to PR $pr; doing nothing"
    fi
done

# vim:set et sts=4:
