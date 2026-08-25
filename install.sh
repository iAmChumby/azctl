#!/bin/sh
# azctl installer (POSIX sh): downloads the single-file app into ~/.local/bin
# as an `azctl` command and verifies it runs. Nothing outside that directory
# is touched by this script; azctl itself bootstraps its own Python deps into
# a private venv on first run.
#
# Usage:
#   ./install.sh            install from the default branch
#   ./install.sh some-ref   install from a specific tag/branch/commit
set -eu

REPO="iAmChumby/azctl"
REF="${1:-${AZCTL_REF:-main}}"
URL="https://raw.githubusercontent.com/%s/%s/azctl.py"

DEST_DIR="${AZCTL_HOME:-$HOME/.local/bin}"
DEST="$DEST_DIR/azctl"

say() { printf '%s\n' "$*"; }
die() { printf 'azctl installer: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required but was not found on PATH."

FETCH=""
if command -v curl >/dev/null 2>&1; then
    FETCH="curl -fsSL"
elif command -v wget >/dev/null 2>&1; then
    FETCH="wget -qO-"
else
    die "either curl or wget is required to download azctl."
fi

mkdir -p "$DEST_DIR"

RAW_URL=$(printf "$URL" "$REPO" "$REF")
say "Downloading azctl ($REF) -> $DEST ..."
$FETCH "$RAW_URL" > "$DEST.tmp" || die "download failed: $RAW_URL"
[ -s "$DEST.tmp" ] || die "downloaded file is empty: $RAW_URL"
chmod +x "$DEST.tmp"
mv "$DEST.tmp" "$DEST"

case ":$PATH:" in
    *":$DEST_DIR:"*) ;;
    *)
        say
        say "NOTE: $DEST_DIR is not on your PATH."
        say "Add it, e.g.:"
        say
        say "    echo 'export PATH=\"$DEST_DIR:\$PATH\"' >> ~/.profile   # or ~/.zshrc / ~/.bashrc"
        say
        ;;
esac

if "$DEST" --help >/dev/null 2>&1; then
    say "Installed: azctl ($REF) at $DEST"
    say "Run 'azctl' for the dashboard, 'azctl status' for a read-only snapshot."
    say "First launch installs Textual+psutil into a private venv (~/.cache/azctl/venv);"
    say "your system Python is never touched."
else
    rm -f "$DEST"
    die "the downloaded azctl did not run correctly; installation aborted."
fi
