#!/bin/bash
[ -d .env2.7 ] && rm -Rf .env2.7
virtualenv .env2.7
. .env2.7/bin/activate
pip install -r requirements.txt
