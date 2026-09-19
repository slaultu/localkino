#!/usr/bin/env bash
# Build "KinoPub Offline.app" into ~/Applications and alias it on the Desktop.
#
# It is an AppleScript applet rather than a shell wrapper for one reason: macOS
# only *activates* an already-running app when you double-click it, so a plain
# wrapper can never react. An applet receives a `reopen` event and can bring the
# browser back instead of doing nothing.
#
# The bundle carries its own copy of the code: macOS refuses Finder-launched
# apps access to ~/Documents, so an app reading this folder would not start.
# Re-run this script after changing the code.
set -euo pipefail
cd "$(dirname "$0")/.."
APPS="$HOME/Applications"
APP="$APPS/KinoPub Offline.app"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "→ drawing the icon"
python3 tools/make_icon.py "$BUILD/icon.png" >/dev/null
ICONSET="$BUILD/icon.iconset"; mkdir -p "$ICONSET"
for spec in "16:16x16" "32:16x16@2x" "32:32x32" "64:32x32@2x" \
            "128:128x128" "256:128x128@2x" "256:256x256" "512:256x256@2x" \
            "512:512x512" "1024:512x512@2x"; do
  px="${spec%%:*}"; name="${spec##*:}"
  sips -z "$px" "$px" "$BUILD/icon.png" --out "$ICONSET/icon_$name.png" >/dev/null 2>&1
done
iconutil -c icns "$ICONSET" -o "$BUILD/icon.icns"

echo "→ compiling the applet"
cat > "$BUILD/applet.applescript" <<'APPLESCRIPT'
on helper(name)
	set here to POSIX path of (path to me)
	return quoted form of (here & "Contents/Resources/" & name)
end helper

on run
	do shell script my helper("start.sh")
end run

-- double-clicked while already running: show the app again, do not restart it
on reopen
	do shell script my helper("start.sh")
end reopen

on quit
	do shell script my helper("stop.sh")
	continue quit
end quit
APPLESCRIPT
rm -rf "$APP"; mkdir -p "$APPS"
osacompile -s -o "$APP" "$BUILD/applet.applescript"

echo "→ adding the code and helpers"
mkdir -p "$APP/Contents/Resources/app"
cp server.py "$APP/Contents/Resources/app/"
cp -R kinopub web "$APP/Contents/Resources/app/"
find "$APP/Contents/Resources/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp "$BUILD/icon.icns" "$APP/Contents/Resources/applet.icns"

cat > "$APP/Contents/Resources/start.sh" <<'START'
#!/bin/bash
# Start the server if it is not up; either way server.py opens the browser.
HERE="$(cd "$(dirname "$0")/app" && pwd)"
LOG="$HOME/Library/Logs/KinoPub Offline.log"
mkdir -p "$(dirname "$LOG")"
cd "$HERE" || exit 1
nohup /usr/bin/python3 server.py >>"$LOG" 2>&1 &
exit 0
START

cat > "$APP/Contents/Resources/stop.sh" <<'STOP'
#!/bin/bash
# Stop only our own server, identified by the instance file it wrote.
PID=$(/usr/bin/python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.config/kinopub-offline/instance.json')))['pid'])" 2>/dev/null || true)
[ -n "${PID:-}" ] && kill "$PID" 2>/dev/null || true
exit 0
STOP
chmod +x "$APP/Contents/Resources/start.sh" "$APP/Contents/Resources/stop.sh"

echo "→ naming the bundle"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName KinoPub Offline" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string KinoPub Offline" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string KinoPub Offline" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier local.kinopub.offline" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string local.kinopub.offline" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string 1.0.0" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
touch "$APP"

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true

echo "→ putting an alias on the Desktop"
DESKTOP="$HOME/Desktop"
if ! osascript >/dev/null 2>&1 <<OSA
tell application "Finder"
  set appFile to POSIX file "$APP" as alias
  set desktopFolder to path to desktop folder as alias
  if exists (item "KinoPub Offline" of desktopFolder) then
    delete (item "KinoPub Offline" of desktopFolder)
  end if
  make new alias file at desktopFolder to appFile
end tell
OSA
then
  rm -f "$DESKTOP/KinoPub Offline"
  ln -s "$APP" "$DESKTOP/KinoPub Offline"
fi

echo
echo "Installed: $APP"
echo "Double-click 'KinoPub Offline' on your Desktop."
