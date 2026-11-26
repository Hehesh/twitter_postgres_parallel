#!/usr/bin/python3

import argparse
import datetime
import io
import json
import zipfile

import sqlalchemy


################################################################################
# Helper functions
################################################################################

def remove_nulls(value):
    """
    PostgreSQL does not allow the null character in strings.
    """
    if value is None:
        return None

    return value.replace("\x00", "")


def get_table_columns(connection, table_name):
    """
    Return the columns that exist in a table.
    """
    sql = sqlalchemy.sql.text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table_name
    """)

    rows = connection.execute(sql, {
        "table_name": table_name
    }).fetchall()

    return {row[0] for row in rows}


def get_id_urls(url, connection):
    """
    Return the ID for a URL, inserting the URL if necessary.
    """
    url = remove_nulls(url)

    sql = sqlalchemy.sql.text("""
        INSERT INTO urls (url)
        VALUES (:url)
        ON CONFLICT DO NOTHING
        RETURNING id_urls
    """)

    result = connection.execute(sql, {
        "url": url
    }).first()

    if result is None:
        sql = sqlalchemy.sql.text("""
            SELECT id_urls
            FROM urls
            WHERE url = :url
        """)

        result = connection.execute(sql, {
            "url": url
        }).first()

    return result[0]


def insert_user(connection, user, id_urls=None):
    """
    Insert or update a user.

    The normalized assignment schema only requires id_users and id_urls.
    """
    user_columns = get_table_columns(connection, "users")

    values = {
        "id_users": user["id"],
        "id_urls": id_urls,
    }

    # These fields are included only if the database schema has them.
    optional_values = {
        "name": user.get("name"),
        "screen_name": user.get("screen_name"),
        "location": user.get("location"),
        "description": user.get("description"),
        "protected": user.get("protected"),
        "followers_count": user.get("followers_count"),
        "friends_count": user.get("friends_count"),
        "listed_count": user.get("listed_count"),
        "favourites_count": user.get("favourites_count"),
        "statuses_count": user.get("statuses_count"),
        "created_at": user.get("created_at"),
        "utc_offset": user.get("utc_offset"),
        "time_zone": user.get("time_zone"),
        "geo_enabled": user.get("geo_enabled"),
        "verified": user.get("verified"),
    }

    for key, value in optional_values.items():
        if key in user_columns:
            values[key] = remove_nulls(value) if isinstance(value, str) else value

    columns = [
        column for column in values
        if column in user_columns
    ]

    insert_columns = ", ".join(columns)
    insert_parameters = ", ".join(":" + column for column in columns)

    updates = [
        f"{column} = EXCLUDED.{column}"
        for column in columns
        if column != "id_users"
    ]

    if updates:
        conflict_clause = "DO UPDATE SET " + ", ".join(updates)
    else:
        conflict_clause = "DO NOTHING"

    sql = sqlalchemy.sql.text(f"""
        INSERT INTO users ({insert_columns})
        VALUES ({insert_parameters})
        ON CONFLICT (id_users) {conflict_clause}
    """)

    connection.execute(sql, {
        column: values[column]
        for column in columns
    })


def insert_simple_row(connection, table_name, values, conflict_columns):
    """
    Insert a row while ignoring duplicate composite-key rows.
    """
    table_columns = get_table_columns(connection, table_name)

    values = {
        key: value
        for key, value in values.items()
        if key in table_columns
    }

    columns = list(values.keys())

    if not columns:
        return

    insert_columns = ", ".join(columns)
    insert_parameters = ", ".join(":" + column for column in columns)

    sql = sqlalchemy.sql.text(f"""
        INSERT INTO {table_name} ({insert_columns})
        VALUES ({insert_parameters})
        ON CONFLICT ({", ".join(conflict_columns)}) DO NOTHING
    """)

    connection.execute(sql, values)


################################################################################
# Tweet insertion
################################################################################

def insert_tweet(connection, tweet):
    """
    Insert one tweet and all related normalized records.
    """
    with connection.begin():
        tweet_columns = get_table_columns(connection, "tweets")

        # Skip tweets that have already been inserted.
        sql = sqlalchemy.sql.text("""
            SELECT id_tweets
            FROM tweets
            WHERE id_tweets = :id_tweets
        """)

        result = connection.execute(sql, {
            "id_tweets": tweet["id"]
        }).first()

        if result is not None:
            return

        ########################################################################
        # Insert the user who created the tweet.
        ########################################################################

        user = tweet["user"]

        if user.get("url") is None:
            user_id_urls = None
        else:
            user_id_urls = get_id_urls(user["url"], connection)

        insert_user(connection, user, user_id_urls)

        ########################################################################
        # Determine tweet text.
        ########################################################################

        try:
            text = tweet["extended_tweet"]["full_text"]
        except KeyError:
            text = tweet.get("text")

        text = remove_nulls(text)

        ########################################################################
        # Determine location information.
        ########################################################################

        geometry_wkt = None
        place_name = None
        country_code = None
        state_code = None

        # Extract place metadata independently of geometry.
        if tweet.get("place") is not None:
            place = tweet["place"]
            place_name = place.get("full_name")

            country_code_value = place.get("country_code")
            if country_code_value is not None:
                country_code = country_code_value.lower()

            if country_code == "us" and place_name is not None:
                state_code = place_name.split(",")[-1].strip().lower()

                if len(state_code) > 2:
                    state_code = None

        # Preserve original geometry priority: geo first, place second.
        if tweet.get("geo") is not None:
            coordinates = tweet["geo"]["coordinates"]
            geometry_wkt = (
                "POINT("
                + str(coordinates[0])
                + " "
                + str(coordinates[1])
                + ")"
            )

        elif tweet.get("place") is not None:
            polygons = tweet["place"]["bounding_box"]["coordinates"]
            polygon_text = []

            for polygon in polygons:
                points = [
                    f"{point[0]} {point[1]}"
                    for point in polygon
                ]

                points.append(
                    f"{polygon[0][0]} {polygon[0][1]}"
                )

                polygon_text.append("(" + ",".join(points) + ")")

            geometry_wkt = "MULTIPOLYGON(" + ",".join(
                "(" + polygon + ")"
                for polygon in polygon_text
            ) + ")"
            
        ########################################################################
        # Ensure reply-to users exist before inserting the tweet.
        ########################################################################

        in_reply_to_user_id = tweet.get("in_reply_to_user_id")

        if in_reply_to_user_id is not None:
            reply_user = {
                "id": in_reply_to_user_id
            }

            insert_user(connection, reply_user)

        ########################################################################
        # Insert the tweet.
        ########################################################################

        tweet_values = {
            "id_tweets": tweet["id"],
            "id_users": tweet["user"]["id"],
            "created_at": tweet.get("created_at"),
            "text": text,
            "source": remove_nulls(tweet.get("source")),
            "truncated": tweet.get("truncated"),
            "in_reply_to_status_id": tweet.get("in_reply_to_status_id"),
            "in_reply_to_user_id": in_reply_to_user_id,
            "quoted_status_id": tweet.get("quoted_status_id"),
            "quoted_status": tweet.get("is_quote_status"),
            "retweeted": tweet.get("retweeted"),
            "favorite_count": tweet.get("favorite_count"),
            "retweet_count": tweet.get("retweet_count"),
            "lang": tweet.get("lang"),
            "place_name": remove_nulls(place_name),
            "country_code": country_code,
            "state_code": state_code,
        }

        tweet_values = {
            key: value
            for key, value in tweet_values.items()
            if key in tweet_columns
        }

        tweet_parameters = {
            key: ":" + key
            for key in tweet_values
        }

        # Handle a possible PostGIS coordinates column separately.
        if "coordinates" in tweet_columns and geometry_wkt is not None:
            tweet_parameters["coordinates"] = (
                "ST_GeomFromText(:coordinates, 4326)"
            )
            tweet_values["coordinates"] = geometry_wkt

        columns = ", ".join(tweet_values.keys())
        parameters = ", ".join(tweet_parameters.values())

        sql = sqlalchemy.sql.text(f"""
            INSERT INTO tweets ({columns})
            VALUES ({parameters})
        """)

        connection.execute(sql, tweet_values)

        ########################################################################
        # Extract URL entities.
        ########################################################################

        try:
            urls = tweet["extended_tweet"]["entities"]["urls"]
        except KeyError:
            urls = tweet.get("entities", {}).get("urls", [])

        for url in urls:
            expanded_url = url.get("expanded_url")

            if expanded_url is None:
                continue

            id_urls = get_id_urls(expanded_url, connection)

            insert_simple_row(
                connection,
                "tweet_urls",
                {
                    "id_tweets": tweet["id"],
                    "id_urls": id_urls,
                },
                ["id_tweets", "id_urls"],
            )

        ########################################################################
        # Extract user mentions.
        ########################################################################

        try:
            mentions = tweet["extended_tweet"]["entities"]["user_mentions"]
        except KeyError:
            mentions = tweet.get("entities", {}).get("user_mentions", [])

        for mention in mentions:
            mentioned_user = {
                "id": mention["id"]
            }

            insert_user(connection, mentioned_user)

            insert_simple_row(
                connection,
                "tweet_mentions",
                {
                    "id_tweets": tweet["id"],
                    "id_users": mention["id"],
                },
                ["id_tweets", "id_users"],
            )

        ########################################################################
        # Extract hashtags and cashtags.
        ########################################################################

        try:
            hashtags = tweet["extended_tweet"]["entities"]["hashtags"]
            cashtags = tweet["extended_tweet"]["entities"]["symbols"]
        except KeyError:
            entities = tweet.get("entities", {})
            hashtags = entities.get("hashtags", [])
            cashtags = entities.get("symbols", [])

        tags = (
            ["#" + hashtag["text"] for hashtag in hashtags] +
            ["$" + cashtag["text"] for cashtag in cashtags]
        )

        for tag in tags:
            insert_simple_row(
                connection,
                "tweet_tags",
                {
                    "id_tweets": tweet["id"],
                    "tag": remove_nulls(tag),
                },
                ["id_tweets", "tag"],
            )

        ########################################################################
        # Extract media.
        ########################################################################

        try:
            media = tweet["extended_tweet"]["extended_entities"]["media"]
        except KeyError:
            try:
                media = tweet["extended_entities"]["media"]
            except KeyError:
                media = []

        for medium in media:
            media_url = medium.get("media_url")

            if media_url is None:
                continue

            id_urls = get_id_urls(media_url, connection)

            insert_simple_row(
                connection,
                "tweet_media",
                {
                    "id_tweets": tweet["id"],
                    "id_urls": id_urls,
                    "type": medium.get("type"),
                },
                ["id_tweets", "id_urls"],
            )


################################################################################
# Main program
################################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--db",
        required=True,
    )

    parser.add_argument(
        "--inputs",
        nargs="+",
        required=True,
    )

    parser.add_argument(
        "--print_every",
        type=int,
        default=1000,
    )

    args = parser.parse_args()

    engine = sqlalchemy.create_engine(
        args.db,
        connect_args={
            "application_name": "load_tweets.py",
        },
    )

    connection = engine.connect()

    try:
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

                        for i, line in enumerate(input_file):
                            tweet = json.loads(line)

                            insert_tweet(connection, tweet)

                            if i % args.print_every == 0:
                                print(
                                    datetime.datetime.now(),
                                    filename,
                                    subfilename,
                                    "i=",
                                    i,
                                    "id=",
                                    tweet["id"],
                                )
    finally:
        connection.close()
        engine.dispose()
