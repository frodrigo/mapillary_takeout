#!/usr/bin/env python3

import argparse
import json
import os
import re
import requests
import sys

# mapillary_tools client_id
CLIENT_ID = "MkJKbDA0bnZuZlcxeTJHTmFqN3g1dzo1YTM0NjRkM2EyZGU5MzBh"

SEQUENCES_PER_PAGE = "100"  # Max from API is 1000, but timeouts
REQUESTS_PER_CALL = 210  # 220 max
SEQUENCE_DL_MAX_RETRIES = 3  # Number of retries for a sequence

API_ENDPOINT = "https://a.mapillary.com"

LOGIN_URL = API_ENDPOINT + "/v2/ua/login?client_id=" + CLIENT_ID
SEQUENCES_URL = (
    API_ENDPOINT
    + "/v3/sequences?client_id="
    + CLIENT_ID
    + "&per_page="
    + SEQUENCES_PER_PAGE
)
MODEL_URL = API_ENDPOINT + "/v3/model.json?client_id=" + CLIENT_ID

AWS_EXPIRED = '<\?xml version="1\.0" encoding="UTF-8"\?>\\n<Error><Code>AccessDenied<\/Code><Message>Request has expired<\/Message>'

DRY_RUN = False


def get_mpy_auth(email, password):
    # Returns mapillary token
    payload = {"email": email, "password": password}
    r = requests.post(LOGIN_URL, json=payload)
    if "token" in r.json():
        r.close()
        return r.json()["token"]
    elif "message" in r.json():
        print("Authentication failed: %s" % r.json()["message"])
    r.close()
    sys.exit(-1)


def get_user_sequences(mpy_token, username, start_date, end_date):
    # Fetches all sequences for the username and
    # returns them in an array of json objects
    # https://www.mapillary.com/developer/api-documentation/#the-sequence-object
    response = []
    nb_images = 0
    headers = {"Authorization": "Bearer " + mpy_token}

    r = requests.get(
        SEQUENCES_URL,
        headers=headers,
        params={"usernames": username, "start_time": start_date, "end_time": end_date},
    )
    for feature in r.json()["features"]:
        response.append(feature)
        nb_images += len(feature["properties"]["coordinateProperties"]["image_keys"])
    r.close()
    nb_seq = len(response)
    while "next" in r.links:
        r = requests.get(r.links["next"]["url"], headers=headers)
        for feature in r.json()["features"]:
            response.append(feature)
            nb_images += len(
                feature["properties"]["coordinateProperties"]["image_keys"]
            )
        r.close()
        nb_seq = len(response)
        print("Fetching %s sequences (%s images) ..." % (nb_seq, nb_images))
    return (response, nb_seq)


def get_source_urls(download_list, mpy_token, username):
    # Fetches "unprocessed original" images URL and returns them in a dict
    source_urls = {}
    chunks = [
        download_list[x : x + REQUESTS_PER_CALL]
        for x in range(0, len(download_list), REQUESTS_PER_CALL)
    ]
    for chunk in chunks:
        params = {
            "paths": json.dumps(
                [["imageByKey", chunk, ["original_url"]]], separators=(",", ":")
            ),
            "method": "get",
        }
        headers = {"Authorization": "Bearer " + mpy_token}
        r = requests.get(MODEL_URL, headers=headers, params=params)
        data = r.json()
        if "jsonGraph" in data:
            for image_key, image in data["jsonGraph"]["imageByKey"].items():
                if "value" in image["original_url"]:
                    source_urls[image_key] = image["original_url"]["value"]
        r.close()
    return source_urls


def download_sequence(output_folder, mpy_token, sequence, username):
    sequence_name = (
        sequence["properties"]["captured_at"]
        + "_"
        + sequence["properties"]["created_at"]
    )
    sequence_day = sequence_name.split("T")[0]
    sorted_folder = output_folder + "/" + sequence_day
    download_list = []
    os.makedirs(sorted_folder, exist_ok=True)

    # First pass on image_keys : sorts which one needs downloading
    image_keys = sequence["properties"]["coordinateProperties"]["image_keys"]
    for image_index, image_key in enumerate(image_keys, 1):
        sorted_path = (
            sorted_folder + "/" + sequence_name + "_" + "%04d" % image_index + ".jpg"
        )
        if not os.path.exists(sorted_path):
            download_list.append(image_key)
        elif os.stat(sorted_path).st_size == 0:
            download_list.append(image_key)

    if not download_list:
        print(" Sequence %r already fully downloaded" % sequence_name)
        return

    already_downloaded = len(image_keys) - len(download_list)
    if already_downloaded:
        print(" Already downloaded: %d/%d" % (already_downloaded, len(image_keys)))

    if DRY_RUN:
        return

    # Second pass : split in chunks and feed into source_urls dict
    source_urls = get_source_urls(download_list, mpy_token, username)

    requests_session = requests.Session()

    # Third pass, download if entry is found in dict
    sequence_dl_retries = 0
    while download_list and not sequence_dl_retries >= SEQUENCE_DL_MAX_RETRIES:
        sequence_dl_retries += 1
        image_index = 0

        if len(download_list) > len(source_urls):
            print(
                " Missing %d/%d images"
                % (len(download_list) - len(source_urls), len(download_list))
            )

        for image_index, image_key in enumerate(image_keys, 1):
            if image_key in download_list:
                sorted_path = (
                    sorted_folder
                    + "/"
                    + sequence_name
                    + "_"
                    + "%04d" % image_index
                    + ".jpg"
                )

                print(
                    "  Downloading image %s #%03d/%03d"
                    % (image_key, image_index, len(image_keys)),
                    end="\r",
                    flush=True,
                )

                if image_key not in source_urls:
                    print(
                        "  No source url for image %s : refreshing source urls"
                        % image_key
                    )
                    source_urls = get_source_urls(download_list, mpy_token, username,)
                    break

                # Get the header first (stream) and compare size
                try:
                    r = requests_session.get(source_urls[image_key], stream=True)
                except requests.exceptions.SSLError:
                    print("  SSL error downloading %r, retrying later" % image_key)
                    break
                except:
                    print(
                        "  Error downloading %r, retrying later. Info %r"
                        % (image_key, sys.exc_info()[0],)
                    )
                    break
                if r.status_code == requests.codes.ok:
                    size = int(r.headers["content-length"])
                    if (
                        os.path.isfile(sorted_path)
                        and os.path.getsize(sorted_path) == size
                    ):
                        print("  Already downloaded as %r" % sorted_path)
                    else:
                        if os.path.isfile(sorted_path):
                            print("  Size mismatch for %r, replacing..." % sorted_path)
                        with open(sorted_path, "wb") as f:
                            f.write(r.content)
                    download_list.remove(image_key)
                elif r.status_code == 403 and re.match(AWS_EXPIRED, r.text):
                    print(" Download token expired, requesting fresh one ...  ")
                    source_urls = get_source_urls(download_list, mpy_token, username,)
                    r.close()
                    # Slow downloads are not an error
                    sequence_dl_retries -= 1
                    break
                else:
                    print(
                        "  Error %r downloading image %r : %r"
                        % (r.status_code, image_key, r.text,)
                    )
                r.close()
    print(" Done downloading sequence %r" % sequence_name)


def main(email, password, username, output_folder, start_date, end_date):
    mpy_token = get_mpy_auth(email, password)
    user_sequences, nb_sequences = get_user_sequences(
        mpy_token, username, start_date, end_date
    )
    for c, sequence in enumerate(reversed(user_sequences), 1):
        print(
            "Sequence %s_%s (%d/%d)"
            % (
                sequence["properties"]["captured_at"],
                sequence["properties"]["created_at"],
                c,
                nb_sequences,
            )
        )
        download_sequence(output_folder, mpy_token, sequence, username)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download your images from Mapillary")
    parser.add_argument("email", help="Your email address for mapillary authentication")
    parser.add_argument("password", help="Your mapillary password")
    parser.add_argument("username", help="Your mapillary username")
    parser.add_argument("output_folder", help="Download destination")
    parser.add_argument(
        "--start-date",
        help="Filter sequences that are captured since this date",
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "--end-date",
        help="Filter sequences that are captured before this date",
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "-D", "--dry-run", action="store_true", help="Check sequences status and leave"
    )
    args = parser.parse_args()

    if args.dry_run:
        DRY_RUN = True

    exit(
        main(
            args.email,
            args.password,
            args.username,
            args.output_folder,
            args.start_date,
            args.end_date,
        )
    )
