import argparse
import subprocess
import sys

def build_common_args(args):
    cmd = [
        "--output", args.output,
        "--scale", str(args.scale),
    ]

    if args.faction:
        cmd += ["--faction", args.faction]
    if args.subFaction:
        cmd += ["--subFaction", args.subFaction]
    if args.headed:
        cmd += ["--headed"]

    return cmd

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-units", action="store_true")
    parser.add_argument("--no-detachments", action="store_true")
    parser.add_argument("--output", default="rendered_cards")
    parser.add_argument("--faction")
    parser.add_argument("--subFaction")
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--headed", action="store_true")

    args = parser.parse_args()

    common = build_common_args(args)

    jobs = []

    if not args.no_units:
        jobs.append([sys.executable, "export_datacards.py", *common])

    if not args.no_detachments:
        jobs.append([sys.executable, "export_detachment_cards.py", *common])

    if not jobs:
        raise SystemExit("Nothing to export: both --no-units and --no-detachments were set")


    processes = [subprocess.Popen(cmd) for cmd in jobs]
    exit_codes = [p.wait() for p in processes]
    raise SystemExit(max(exit_codes))

if __name__ == "__main__":
    main()