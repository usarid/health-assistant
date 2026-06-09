#!/usr/bin/env bash
# Day-one setup. Run AFTER Xcode has finished installing from the App Store.
# Idempotent — safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGED="$REPO_ROOT/mobile-staged"
PROJECT="$REPO_ROOT/mobile"

cd "$REPO_ROOT"

echo "=== 1/6: verify prereqs ==="
command -v flutter > /dev/null || { echo "ERROR: flutter not on PATH (brew install --cask flutter)"; exit 1; }
command -v pod > /dev/null || { echo "ERROR: cocoapods not on PATH (brew install cocoapods)"; exit 1; }
[ -d /Applications/Xcode.app ] || { echo "ERROR: /Applications/Xcode.app not found — install Xcode from the Mac App Store first"; exit 1; }
echo "  flutter: $(flutter --version | head -1)"
echo "  pod:     $(pod --version)"
echo "  Xcode:   $(/usr/bin/defaults read /Applications/Xcode.app/Contents/Info CFBundleShortVersionString 2>/dev/null || echo '?')"

echo ""
echo "=== 2/6: Xcode post-install (xcode-select + license + first-launch) ==="
if [ "$(xcode-select -p)" != "/Applications/Xcode.app/Contents/Developer" ]; then
  echo "  switching xcode-select to /Applications/Xcode.app — will sudo prompt"
  sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
fi
echo "  accepting Xcode license (will sudo prompt if not yet accepted)"
sudo xcodebuild -license accept || true
echo "  running first-launch installer (idempotent)"
sudo xcodebuild -runFirstLaunch || true

echo ""
echo "=== 3/6: flutter doctor (no Android, iOS only for this prototype) ==="
flutter config --no-enable-android > /dev/null
flutter config --enable-macos-desktop > /dev/null
flutter doctor

echo ""
echo "=== 4/6: scaffold project (skipped if mobile/ already exists) ==="
if [ -d "$PROJECT" ]; then
  echo "  $PROJECT already exists — skipping flutter create"
else
  flutter create --org com.binahealth --project-name bina_mobile \
    --platforms ios,macos --no-pub "$PROJECT"
fi

echo ""
echo "=== 5/6: apply staged Dart code + pubspec ==="
# Overlay our staged files onto the generated project. Preserves the
# auto-generated iOS Runner.xcodeproj / macos/ / etc.
cp "$STAGED/pubspec.yaml" "$PROJECT/pubspec.yaml"
rm -f "$PROJECT/lib/main.dart"
cp -R "$STAGED/lib/." "$PROJECT/lib/"
echo "  pubspec.yaml + lib/ overlaid"

echo ""
echo "=== 6/6: flutter pub get + boot iOS Simulator + run ==="
cd "$PROJECT"
flutter pub get

# Boot the simulator if no iOS simulator is currently running.
if ! xcrun simctl list devices booted | grep -q "iPhone"; then
  # Find first available iPhone simulator
  DEVICE_ID=$(xcrun simctl list devices available | grep -E "iPhone (15|16|17)" | head -1 | grep -oE '\([A-F0-9-]{36}\)' | tr -d '()')
  if [ -z "$DEVICE_ID" ]; then
    echo "  no iPhone 15/16/17 simulator found — listing all available:"
    xcrun simctl list devices available | grep iPhone
    exit 1
  fi
  echo "  booting iPhone simulator ($DEVICE_ID)"
  xcrun simctl boot "$DEVICE_ID" 2>/dev/null || true
  open -a Simulator
  sleep 4
fi

echo ""
echo "=== Ready! Running on iOS Simulator. ==="
echo "  cd $PROJECT && flutter run -d 'iPhone'"
echo ""
echo "  (or:   flutter run -d <specific-device-id>)"
flutter run -d iPhone
