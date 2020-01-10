#!/usr/bin/env bash

(trap 'kill 0' SIGINT SIGHUP; python3 -m pipenv run python app.py & python3 -m pipenv run python bgcompute.py & yarn --cwd arlo-client start)
