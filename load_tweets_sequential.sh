#!/bin/bash

set -e

export PGPASSWORD=pass

files=$(find data -type f -print)

echo '================================================================================'
echo 'load denormalized'
echo '================================================================================'

time for file in $files; do
    echo "Loading $file"

    unzip -p "$file" |
        python3 -c '
import sys

for line in sys.stdin:
    line = line.replace(r"\u0000", "")
    line = line.replace("\x00", "")
    sys.stdout.write(line)
' |
        psql \
            --host localhost \
            --port 1380 \
            --username postgres \
            --dbname postgres \
            --command "COPY tweets_jsonb (data) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', QUOTE E'\x01', ESCAPE E'\x02')"
done

echo '================================================================================'
echo 'load pg_normalized'
echo '================================================================================'

time for file in $files; do
    echo "Loading $file"

    python3 -u load_tweets.py \
        --db "postgresql://postgres:pass@localhost:8310/postgres" \
        --inputs "$file"
done

echo '================================================================================'
echo 'load pg_normalized_batch'
echo '================================================================================'

time for file in $files; do
    echo "Loading $file"

    python3 -u load_tweets_batch.py \
        --db "postgresql://postgres:pass@localhost:3810/postgres" \
        --inputs "$file"
done
