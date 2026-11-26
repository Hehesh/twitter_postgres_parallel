#!/usr/bin/python3

import argparse
import datetime
import io
import json
import zipfile

import sqlalchemy


def remove_nulls(value):
    """
    Replace null characters because PostgreSQL does not support them.
    """
    if value is None:
        return None

    return value.replace("\x00", "\\x00")


def batch(iterable, size):
    """
    Group a list into batches.
    """
    for start in range(0, len(iterable), size):
        yield iterable[start:start + size]


def _bulk_insert_sql(table, rows):
    """
    Generate a bulk INSERT statement and bind parameters.
    """
    if not rows:
        raise ValueError("rows must contain at least one row")

    keys = list(rows[0].keys())

    for row in rows:
        if set(row.keys()) != set(keys):
            raise ValueError(
                "All dictionaries must contain the same keys"
            )

    columns = ",".join(keys)

    values = ",".join(
        "(" + ",".join(
            f":{key}{index}"
            for key in keys
        ) + ")"
        for index in range(len(rows))
    )

    sql = f"""
        INSERT INTO {table} ({columns})
        VALUES {values}
        ON CONFLICT DO NOTHING
    """

    binds = {
        f"{key}{index}": value
        for index, row in enumerate(rows)
        for key, value in row.items()
    }

    return " ".join(sql.split()), binds


def bulk_insert(connection, table, rows):
    """
    Insert a list of dictionaries into a table.
    """
    if not rows:
        return

    sql, binds = _bulk_insert_sql(table, rows)
    connection.execute(sqlalchemy.text(sql), binds)


def _get_entities(tweet):
    """
    Return the tweet entity object.

    Extended tweet entities take precedence over regular entities.
    """
    extended_tweet = tweet.get("extended_tweet")

    if extended_tweet:
        return extended_tweet.get("entities", {})

    return tweet.get("entities", {})


def _get_media(tweet):
    """
    Return media from extended_entities when available.
    """
    extended_tweet = tweet.get("extended_tweet")

    if extended_tweet:
        extended_entities = extended_tweet.get(
            "extended_entities",
            {},
        )

        if extended_entities.get("media"):
            return extended_entities["media"]

    extended_entities = tweet.get("extended_entities", {})
    return extended_entities.get("media", [])


def _get_tweet_text(tweet):
    """
    Return the full tweet text when available.
    """
    extended_tweet = tweet.get("extended_tweet")

    if extended_tweet and "full_text" in extended_tweet:
        return extended_tweet["full_text"]

    return tweet.get("text")


def _get_geo(tweet):
    """
    Return a tuple containing the WKT geometry type and coordinates.
    """
    geo = tweet.get("geo")

    if geo and geo.get("coordinates"):
        coordinates = geo["coordinates"]

        return (
            "POINT",
            f"{coordinates[0]} {coordinates[1]}",
        )

    place = tweet.get("place")

    if not place:
        return None, None

    bounding_box = place.get("bounding_box")

    if not bounding_box:
        return None, None

    polygons = []

    for polygon in bounding_box.get("coordinates", []):
        points = [
            f"{point[0]} {point[1]}"
            for point in polygon
        ]

        if not points:
            continue

        if points[0] != points[-1]:
            points.append(points[0])

        polygons.append("(" + ",".join(points) + ")")

    if not polygons:
        return None, None

    return (
        "MULTIPOLYGON",
        "(" + ",".join(polygons) + ")",
    )


def _get_place_values(tweet):
    """
    Return place_name, country_code, and state_code.
    """
    place = tweet.get("place")

    if not place:
        return None, None, None

    place_name = place.get("full_name")

    country_code = place.get("country_code")

    if country_code:
        country_code = country_code.lower()

    state_code = None

    if country_code == "us" and place_name:
        state_code = place_name.split(",")[-1].strip().lower()

        if len(state_code) > 2:
            state_code = None

    return place_name, country_code, state_code


def _get_url_ids(connection, urls):
    """
    Insert URLs into the normalized urls table and return a mapping from
    URL text to id_urls.

    This function must be called inside an active transaction.
    """
    if not urls:
        return {}

    unique_urls = list(dict.fromkeys(urls))

    bulk_insert(
        connection,
        "urls",
        [{"url": url} for url in unique_urls],
    )

    url_ids = {}

    select_sql = sqlalchemy.text(
        """
        SELECT id_urls
        FROM urls
        WHERE url = :url
        """
    )

    for url in unique_urls:
        row = connection.execute(
            select_sql,
            {"url": url},
        ).first()

        if row is None:
            raise RuntimeError(
                f"Unable to resolve URL id for {url!r}"
            )

        url_ids[url] = row[0]

    return url_ids


def _insert_tweets(connection, input_tweets):
    users = []
    tweets = []
    users_from_replies = []
    users_from_mentions = []
    tweet_mentions = []
    tweet_tags = []

    tweet_url_pairs = []
    tweet_media_pairs = []
    normalized_urls = []

    for tweet in input_tweets:
        user = tweet["user"]

        users.append(
            {
                "id_users": user["id"],
                "created_at": user["created_at"],
                "updated_at": tweet["created_at"],
                "screen_name": remove_nulls(
                    user.get("screen_name")
                ),
                "name": remove_nulls(user.get("name")),
                "location": remove_nulls(user.get("location")),
                "description": remove_nulls(
                    user.get("description")
                ),
                "protected": user.get("protected"),
                "verified": user.get("verified"),
                "friends_count": user.get("friends_count"),
                "listed_count": user.get("listed_count"),
                "favourites_count": user.get(
                    "favourites_count"
                ),
                "statuses_count": user.get("statuses_count"),
                "withheld_in_countries": user.get(
                    "withheld_in_countries"
                ),
            }
        )

        geo_str, geo_coords = _get_geo(tweet)

        place_name, country_code, state_code = (
            _get_place_values(tweet)
        )

        text = _get_tweet_text(tweet)

        in_reply_to_user_id = tweet.get(
            "in_reply_to_user_id"
        )

        if in_reply_to_user_id is not None:
            users_from_replies.append(
                {
                    "id_users": in_reply_to_user_id,
                    "screen_name": remove_nulls(
                        tweet.get("in_reply_to_screen_name")
                    ),
                }
            )

        tweets.append(
            {
                "id_tweets": tweet["id"],
                "id_users": user["id"],
                "created_at": tweet["created_at"],
                "in_reply_to_status_id": tweet.get(
                    "in_reply_to_status_id"
                ),
                "in_reply_to_user_id": in_reply_to_user_id,
                "quoted_status_id": tweet.get(
                    "quoted_status_id"
                ),
                "geo_str": geo_str,
                "geo_coords": geo_coords,
                "retweet_count": tweet.get("retweet_count"),
                "quote_count": tweet.get("quote_count"),
                "favorite_count": tweet.get(
                    "favorite_count"
                ),
                "withheld_copyright": tweet.get(
                    "withheld_copyright"
                ),
                "withheld_in_countries": tweet.get(
                    "withheld_in_countries"
                ),
                "place_name": place_name,
                "country_code": country_code,
                "state_code": state_code,
                "lang": tweet.get("lang"),
                "text": remove_nulls(text),
                "source": remove_nulls(tweet.get("source")),
            }
        )

        entities = _get_entities(tweet)

        for url_entity in entities.get("urls", []):
            expanded_url = url_entity.get("expanded_url")

            if not expanded_url:
                continue

            expanded_url = remove_nulls(expanded_url)

            tweet_url_pairs.append(
                {
                    "id_tweets": tweet["id"],
                    "url": expanded_url,
                }
            )

            normalized_urls.append(expanded_url)

        for mention in entities.get("user_mentions", []):
            mention_id = mention.get("id")

            if mention_id is None:
                continue

            users_from_mentions.append(
                {
                    "id_users": mention_id,
                    "name": remove_nulls(
                        mention.get("name")
                    ),
                    "screen_name": remove_nulls(
                        mention.get("screen_name")
                    ),
                }
            )

            tweet_mentions.append(
                {
                    "id_tweets": tweet["id"],
                    "id_users": mention_id,
                }
            )

        hashtags = entities.get("hashtags", [])
        symbols = entities.get("symbols", [])

        tags = (
            ["#" + hashtag["text"] for hashtag in hashtags]
            + ["$" + symbol["text"] for symbol in symbols]
        )

        for tag in tags:
            tweet_tags.append(
                {
                    "id_tweets": tweet["id"],
                    "tag": remove_nulls(tag),
                }
            )

        for medium in _get_media(tweet):
            media_url = medium.get("media_url")

            if not media_url:
                continue

            media_url = remove_nulls(media_url)

            tweet_media_pairs.append(
                {
                    "id_tweets": tweet["id"],
                    "url": media_url,
                    "type": medium.get("type"),
                }
            )

            normalized_urls.append(media_url)

    with connection.begin():
        bulk_insert(connection, "users", users)
        bulk_insert(connection, "users", users_from_replies)
        bulk_insert(connection, "users", users_from_mentions)

        if tweets:
            value_rows = []

            for index in range(len(tweets)):
                value_rows.append(
                    f"""
                    (
                        :id_tweets{index},
                        :id_users{index},
                        :created_at{index},
                        :in_reply_to_status_id{index},
                        :in_reply_to_user_id{index},
                        :quoted_status_id{index},
                        ST_GeomFromText(
                            CAST(:geo_str{index} AS text)
                            || '('
                            || CAST(:geo_coords{index} AS text)
                            || ')'
                        ),
                        :retweet_count{index},
                        :quote_count{index},
                        :favorite_count{index},
                        :withheld_copyright{index},
                        :withheld_in_countries{index},
                        :place_name{index},
                        :country_code{index},
                        :state_code{index},
                        :lang{index},
                        :text{index},
                        :source{index}
                    )
                    """
                )

            tweet_sql = sqlalchemy.text(
                """
                INSERT INTO tweets (
                    id_tweets,
                    id_users,
                    created_at,
                    in_reply_to_status_id,
                    in_reply_to_user_id,
                    quoted_status_id,
                    geo,
                    retweet_count,
                    quote_count,
                    favorite_count,
                    withheld_copyright,
                    withheld_in_countries,
                    place_name,
                    country_code,
                    state_code,
                    lang,
                    text,
                    source
                )
                VALUES
                """
                + ",".join(value_rows)
                + """
                ON CONFLICT DO NOTHING
                """
            )

            tweet_binds = {
                f"{key}{index}": value
                for index, tweet in enumerate(tweets)
                for key, value in tweet.items()
            }

            connection.execute(tweet_sql, tweet_binds)

        url_ids = _get_url_ids(
            connection,
            normalized_urls,
        )

        tweet_urls = [
            {
                "id_tweets": row["id_tweets"],
                "id_urls": url_ids[row["url"]],
            }
            for row in tweet_url_pairs
        ]

        tweet_media = [
            {
                "id_tweets": row["id_tweets"],
                "id_urls": url_ids[row["url"]],
                "type": row["type"],
            }
            for row in tweet_media_pairs
        ]

        bulk_insert(
            connection,
            "tweet_mentions",
            tweet_mentions,
        )

        bulk_insert(
            connection,
            "tweet_tags",
            tweet_tags,
        )

        bulk_insert(
            connection,
            "tweet_media",
            tweet_media,
        )

        bulk_insert(
            connection,
            "tweet_urls",
            tweet_urls,
        )


def insert_tweets(connection, tweets, batch_size=1000):
    """
    Insert tweets in batches.
    """
    for index, tweet_batch in enumerate(
        batch(tweets, batch_size)
    ):
        print(
            datetime.datetime.now(),
            "insert_tweets i=",
            index,
        )

        _insert_tweets(connection, tweet_batch)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--db", required=True)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument(
        "--batch_size",
        type=int,
        default=1000,
    )

    args = parser.parse_args()

    engine = sqlalchemy.create_engine(
        args.db,
        connect_args={
            "application_name": (
                "load_tweets_batch.py --inputs "
                + " ".join(args.inputs)
            )
        },
    )

    with engine.connect() as connection:
        for filename in sorted(args.inputs, reverse=True):
            print(datetime.datetime.now(), filename)

            with zipfile.ZipFile(filename, "r") as archive:
                for subfilename in sorted(
                    archive.namelist(),
                    reverse=True,
                ):
                    with io.TextIOWrapper(
                        archive.open(subfilename)
                    ) as input_file:
                        tweets = [
                            json.loads(line)
                            for line in input_file
                            if line.strip()
                        ]

                    insert_tweets(
                        connection,
                        tweets,
                        args.batch_size,
                    )


if __name__ == "__main__":
    main()
