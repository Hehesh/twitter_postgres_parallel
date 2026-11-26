#!/bin/bash

set -e

export PGPASSWORD=pass

files=$(find data -type f -print)

echo '================================================================================'
echo 'load denormalized'
echo '================================================================================'

time (
    printf '%s\n' "$files" |
        parallel --halt soon,fail=1 ./load_denormalized.sh
)

echo '================================================================================'
echo 'load pg_normalized'
echo '================================================================================'

time (
    printf '%s\n' "$files" |
        parallel --halt soon,fail=1 \
            python3 -u load_tweets.py \
            --db "postgresql://postgres:pass@localhost:8310/postgres" \
            --inputs
)

echo '================================================================================'
echo 'load pg_normalized_batch'
echo '================================================================================'

time (
    printf '%s\n' "$files" |
        parallel --halt soon,fail=1 \
            python3 -u load_tweets_batch.py \
            --db "postgresql://postgres:pass@localhost:3810/postgres" \
            --inputs
)
