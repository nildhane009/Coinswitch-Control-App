# CoinSwitch Control — integrated build package

Contains the supplied `coinswitch_v3.py`, a localhost Termux bridge, and a native Jetpack Compose Android client.

IMPORTANT: the bridge must share the SAME Python process as the running bot to change its in-memory globals. The bridge is localhost-only.

Android: open `android/` in Android Studio and Build > Build APK(s).

Bridge endpoint: `http://127.0.0.1:8787`

Controls exposed:
- status / positions / logs
- master orders switch
- PAPER / LIVE / OFF mode
- low-capital mode
- trailing stop
- live recovery
- emergency exit all

The existing bot remains the execution engine and its existing protection/recovery logic is not replaced.
