#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
: "${PROBE_ANDROID_JAR:?Set PROBE_ANDROID_JAR to API 35 android.jar}"
: "${PROBE_BUILD_TOOLS:?Set PROBE_BUILD_TOOLS to build-tools 35.0.0}"
BUILD_DIR="${PROBE_BUILD_DIR:-$PWD/build}"
mkdir -p "$BUILD_DIR/classes" "$BUILD_DIR/dex" "$BUILD_DIR/tests"
if command -v javac >/dev/null 2>&1; then
  COMPILER=(javac --release 8 -encoding UTF-8)
else
  : "${PROBE_ECJ_JAR:?Set PROBE_ECJ_JAR to Eclipse ECJ 3.40.0 when javac is unavailable}"
  COMPILER=(java -jar "$PROBE_ECJ_JAR" -1.8 -encoding UTF-8 -bootclasspath "$PROBE_ANDROID_JAR:$PROBE_BUILD_TOOLS/core-lambda-stubs.jar")
fi
"${COMPILER[@]}" -d "$BUILD_DIR/tests" src/ir/tradeonbu/probe/ChannelFilter.java tests/ChannelFilterTest.java
java -cp "$BUILD_DIR/tests" ir.tradeonbu.probe.ChannelFilterTest
"${COMPILER[@]}" -classpath "$PROBE_ANDROID_JAR" -d "$BUILD_DIR/classes" src/ir/tradeonbu/probe/*.java
python3 - "$BUILD_DIR" <<'PYZIP'
from pathlib import Path
import sys, zipfile
root = Path(sys.argv[1])
with zipfile.ZipFile(root / "classes.jar", "w", zipfile.ZIP_DEFLATED) as out:
    for p in (root / "classes").rglob("*.class"):
        out.write(p, p.relative_to(root / "classes"))
PYZIP
"$PROBE_BUILD_TOOLS/d8" --lib "$PROBE_ANDROID_JAR" --min-api 26 --output "$BUILD_DIR/dex" "$BUILD_DIR/classes.jar"
"$PROBE_BUILD_TOOLS/aapt2" link -I "$PROBE_ANDROID_JAR" --manifest AndroidManifest.xml --min-sdk-version 26 --target-sdk-version 35 -o "$BUILD_DIR/unsigned.apk"
zip -q -j "$BUILD_DIR/unsigned.apk" "$BUILD_DIR/dex/classes.dex"
"$PROBE_BUILD_TOOLS/zipalign" -f 4 "$BUILD_DIR/unsigned.apk" "$BUILD_DIR/aligned.apk"
# Local diagnostic signing only. Do not reuse this public-password key for production.
if [[ ! -f "$BUILD_DIR/probe-test.jks" ]]; then
  keytool -genkeypair -keystore "$BUILD_DIR/probe-test.jks" -storepass android -keypass android -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 3650 -dname "CN=TradeOnBU Diagnostic,O=Local Test,C=DE" >/dev/null 2>&1
fi
"$PROBE_BUILD_TOOLS/apksigner" sign --ks "$BUILD_DIR/probe-test.jks" --ks-pass pass:android --out "$BUILD_DIR/TradeOnBU-Notification-Probe-0.1.apk" "$BUILD_DIR/aligned.apk"
"$PROBE_BUILD_TOOLS/apksigner" verify --verbose "$BUILD_DIR/TradeOnBU-Notification-Probe-0.1.apk"
