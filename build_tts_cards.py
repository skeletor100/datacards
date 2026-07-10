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

ROOT_DIR = "./rendered_cards"

GITHUB_USER = "skeletor100"
GITHUB_REPO = "datacards"
GITHUB_BRANCH = "main/rendered_cards"

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
# The longest edge of every tile will have this TTS transform scale.
# TTS creates custom tiles with the short image edge as its base unit, so
# wide/tall images need a smaller uniform scale as their aspect ratio grows.
TILE_LONG_EDGE_SCALE = 3.0

def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia faction extractor")
    parser.add_argument("--faction", help="Faction name, e.g. 'Adeptus Astartes'")
    parser.add_argument(
        "--no-git-refresh",
        action="store_true",
        help="Do not upload to git"
    )
    return parser.parse_args()


# =========================
# IMAGE → TTS TOKEN
# =========================

# =========================
# IMAGE → TTS TILE
# =========================

def get_normalized_tile_scale(image_path):
    """Return a uniform scale that gives every tile the same longest edge."""
    with Image.open(image_path) as image:
        width, height = image.size

    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid image dimensions for {image_path}: {width}x{height}")

    aspect_ratio = max(width, height) / min(width, height)
    return TILE_LONG_EDGE_SCALE / aspect_ratio


def create_tile(image_path, url):
    tile_scale = get_normalized_tile_scale(image_path)

    return {
        "GUID": guid(),
        "Name": "Custom_Tile",
        "Nickname": os.path.splitext(os.path.basename(image_path))[0],

        "Transform": {
            "posX": 0,
            "posY": 1,
            "posZ": 0,
            "rotX": 0,
            "rotY": 180,
            "rotZ": 0,
            "scaleX": tile_scale,
            "scaleY": tile_scale,
            "scaleZ": tile_scale
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

            # back of tile
            "ImageSecondaryURL": url,

            "ImageScalar": 1.0,
            "WidthScale": 0.0,

            "CustomTile": {
                "Type": 0,
                "Thickness": 0.1,
                "Stackable": False,
                "Stretch": True
            }
        }
    }

def create_container(name):
    return {
        "GUID": guid(),
        "Name": "Bag",
        "Nickname": name,

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

# =========================
# RECURSIVE FOLDER BUILD
# =========================

def build_container(folder):

    container = create_container(os.path.basename(folder))

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
                create_tile(
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

    if not args.no_git_refresh:
        git_publish_all()

    root_objects = []

    base_container = create_container("40kDatacards")

    for item in sorted(os.listdir(ROOT_DIR)):

        if item in EXCLUDED_DIRS:
            continue

        path = os.path.join(ROOT_DIR, item)

        if not os.path.isdir(path):
            continue

        if faction_filter and faction_filter not in path:
            continue

        base_container["ContainedObjects"].append(
            build_container(path)
        )

    root_objects.append(base_container)

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