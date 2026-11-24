#!/bin/bash
# Install requirements.txt and PyInstaller in the active Python first.
# Usage: bash packaging/build_macos.sh [arm64|x86_64]
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
    echo "This installer must be built on macOS." >&2
    exit 2
fi
native_arch=$(uname -m)
arch=${1:-$native_arch}
python_bin=${PYTHON_BIN:-python}
if [[ $# -gt 1 || "$arch" != "$native_arch" || ( "$arch" != arm64 && "$arch" != x86_64 ) ]]; then
    echo "Use a native arm64 or x86_64 runner matching the requested architecture." >&2
    exit 2
fi
if [[ "$("$python_bin" -c 'import platform; print(platform.machine())')" != "$arch" ]]; then
    echo "The active Python interpreter must run as $arch." >&2
    exit 2
fi

root=$(cd "$(dirname "$0")/.." && pwd)
dist="$root/dist"
mkdir -p "$dist"
work=$(mktemp -d "$dist/.maildesk-build.XXXXXX")
mountpoint="$work/mount"
mounted=false
qa_profile=
cleanup() {
    local code=$?
    if [[ -n "$qa_profile" ]]; then
        rm -rf -- "$qa_profile"
    fi
    if [[ "$mounted" == true ]] && ! hdiutil detach "$mountpoint"; then
        echo "Disk image still mounted at $mountpoint; preserving temporary files." >&2
        exit 1
    fi
    rm -rf -- "$work"
    exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

app_name="个人邮件助手.app"
filename="PersonalMailAssistant-macOS-$arch.dmg"
mkdir -p "$mountpoint" "$work/installed"
PYINSTALLER_CONFIG_DIR="$work/cache" "$python_bin" -m PyInstaller --clean --noconfirm \
    --workpath "$work/build" --distpath "$work/image-root" "$root/packaging/macos.spec"
ln -s /Applications "$work/image-root/Applications"
hdiutil create -fs HFS+ -format UDZO -volname "个人邮件助手" \
    -srcfolder "$work/image-root" "$work/$filename"
hdiutil verify "$work/$filename"
hdiutil attach -readonly -nobrowse -noautoopen -mountpoint "$mountpoint" "$work/$filename"
mounted=true
[[ "$(readlink "$mountpoint/Applications")" == /Applications ]]
installed="$work/installed/$app_name"
ditto "$mountpoint/$app_name" "$installed"
hdiutil detach "$mountpoint"
mounted=false

contents="$installed/Contents"
exe="$contents/MacOS/PersonalMailAssistant"
codesign --verify --deep --strict "$installed"
[[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$contents/Info.plist")" == "$(< "$root/packaging/VERSION")" ]]
[[ "$(lipo -archs "$exe")" == "$arch" ]]
for framework in QtCore QtWebEngineCore; do
    lipo "$contents/Frameworks/PySide6/Qt/lib/$framework.framework/$framework" -verify_arch "$arch"
done

# Keep the local socket path short and isolate both Qt's application data and native preferences.
qa_profile=$(mktemp -d /private/tmp/mailqa.XXXXXX)
HOME="$qa_profile" CFFIXED_USER_HOME="$qa_profile" TMPDIR="$qa_profile" QT_QPA_PLATFORM=offscreen \
    "$python_bin" - "$exe" "$qa_profile" <<'PY'
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths
from PySide6.QtNetwork import QLocalSocket

exe, profile = sys.argv[1:]
app = QCoreApplication([])
app.setApplicationName("PersonalMailAssistant")
data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)).resolve()
settings = Path(QSettings("PersonalMailAssistant", "ReplyModels").fileName()).resolve()
if not all(path.is_relative_to(profile) for path in (data_dir, settings)):
    raise RuntimeError("The test application profile is not isolated")
primary = subprocess.Popen([exe])
try:
    database = data_dir / "accounts.sqlite3"
    until = time.monotonic() + 45
    while time.monotonic() < until and primary.poll() is None and not database.is_file():
        time.sleep(0.1)
    if not database.is_file() or primary.poll() is not None:
        raise RuntimeError("Installed application did not initialize an empty profile")

    # The main window's Qt event loop handles and closes the activation connection.
    peer = QLocalSocket()
    key = hashlib.sha256(os.fsencode(str(data_dir))).hexdigest()[:32]
    peer.connectToServer("personal-mail-" + key)
    if not peer.waitForConnected(3000):
        raise RuntimeError(peer.errorString())
    if peer.state() != QLocalSocket.UnconnectedState and not peer.waitForDisconnected(15000):
        raise RuntimeError("The installed window did not process activation")
    db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        if db.execute("SELECT count(*) FROM accounts").fetchone() != (0,):
            raise RuntimeError("The installation did not start with an empty inbox")
    finally:
        db.close()
    subprocess.run([exe], check=True, timeout=20)
    if primary.poll() is not None:
        raise RuntimeError("The first instance exited during the second launch")
finally:
    if primary.poll() is None:
        primary.terminate()
        try:
            primary.wait(timeout=8)
        except subprocess.TimeoutExpired:
            primary.kill()
            primary.wait()
PY

# The temporary file lives on the same filesystem; replace existing output only after verification.
mv -f "$work/$filename" "$dist/$filename"
shasum -a 256 "$dist/$filename"
