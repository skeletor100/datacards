import os
import json
import base64
import requests
import argparse
import subprocess
from PIL import Image
import uuid

# =========================
# CONFIG
# =========================

ROOT_DIR = "."

GITHUB_USER = "skeletor100"
GITHUB_REPO = "datacards"
GITHUB_BRANCH = "main"

# create a GitHub personal access token
import os

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg"
}

EXCLUDED_DIRS = {
    ".git",
    "__pycache__"
}

def guid():
    return str(uuid.uuid4())[:6]

def git_publish_all():

    subprocess.run(
        ["git", "add", "."],
        check=True
    )

    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"]
    )

    if status.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", "update images"],
            check=True
        )
    else:
        print("No changes to commit")

    subprocess.run(
        [
            "git",
            "push",
            f"https://{GITHUB_TOKEN}@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
        ],
        check=True
    )

# TTS units are not pixels.
# Adjust once you see them in-game.
PIXEL_TO_TTS_SCALE = 0.002

def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia faction extractor")
    parser.add_argument("--faction", help="Faction name, e.g. 'Adeptus Astartes'")
    return parser.parse_args()


# =========================
# IMAGE → TTS TOKEN
# =========================

def create_token(image_path, url):

    img = Image.open(image_path)
    width, height = img.size

    return {
        "GUID": guid(),
        "Name": "Custom_Token",
        "Nickname": os.path.splitext(os.path.basename(image_path))[0],

        "Transform": {
            "posX": 0,
            "posY": 1,
            "posZ": 0,
            "rotX": 0,
            "rotY": 180,
            "rotZ": 0,
            "scaleX": 1,
            "scaleY": 1,
            "scaleZ": 1
        },

        "ColorDiffuse": {
            "r": 1,
            "g": 1,
            "b": 1
        },

        "Locked": False,
        "Grid": True,
        "Snap": True,
        "DragSelectable": True,
        "Tooltip": True,

        "CustomImage": {
            "ImageURL": url,
            "ImageSecondaryURL": "",
            "ImageScalar": 1.0,
            "WidthScale": 0.0,

            "CustomToken": {
                "Thickness": 0.2,
                "MergeDistancePixels": 15.0,
                "StandUp": False,
                "Stackable": False
            }
        }
    }


# =========================
# RECURSIVE FOLDER BUILD
# =========================

def build_container(folder):

    container = {
        "GUID": guid(),
        "Name": "Bag",
        "Nickname": os.path.basename(folder),

        "Transform": {
            "posX": 0,
            "posY": 0,
            "posZ": 0,
            "rotX": 0,
            "rotY": 0,
            "rotZ": 0,
            "scaleX": 1,
            "scaleY": 1,
            "scaleZ": 1
        },

        "ColorDiffuse": {
            "r": 0.7058823,
            "g": 0.366520882,
            "b": 0.0
        },

        "Locked": False,
        "Grid": True,
        "Snap": True,
        "Sticky": True,
        "DragSelectable": True,
        "Tooltip": True,

        "Bag": {
            "Order": 0
        },

        "ContainedObjects": []
    }

    for item in sorted(os.listdir(folder)):

        path = os.path.join(folder, item)

        if os.path.isdir(path):

            container["ContainedObjects"].append(
                build_container(path)
            )

        elif os.path.splitext(item)[1].lower() in IMAGE_EXTENSIONS:

            relative = os.path.relpath(
                path,
                ROOT_DIR
            )

            repo_path = relative.replace("\\", "/")

            image_url = (
                f"https://raw.githubusercontent.com/"
                f"{GITHUB_USER}/{GITHUB_REPO}/"
                f"{GITHUB_BRANCH}/{repo_path}"
            )

            container["ContainedObjects"].append(
                create_token(
                    path,
                    image_url
                )
            )

    return container


# =========================
# MAIN
# =========================

if __name__ == "__main__":

    args = parse_args()

    faction_filter = args.faction.strip().upper().replace(" ", "_") if args.faction else None

    git_publish_all()

    root_objects = []

    for item in sorted(os.listdir(ROOT_DIR)):

        if item in EXCLUDED_DIRS:
            continue

        path = os.path.join(ROOT_DIR, item)

        if not os.path.isdir(path):
            continue

        if faction_filter and faction_filter not in path:
            continue



        root_objects.append(
            build_container(path)
        )


    tts_save = {
        "ObjectStates": root_objects
    }


    with open(
        "datacards_tts.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            tts_save,
            f,
            indent=4
        )


    print("Generated datacards_tts.json")