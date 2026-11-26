#!/bin/sh

set -e

export PGPASSWORD=pass

file=$1

if [ -z "$file" ]; then
    echo "Usage: $0 DATA_FILE" >&2
    exit 1
fi

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
