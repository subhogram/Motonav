MotoNav — dual transport (Wi-Fi + Bluetooth)
=============================================

The Pi listens on BOTH at once. The phone can use either.

CONNECTION MODES (in the app)
  AUTO       keeps both alive, prefers Wi-Fi, falls back to Bluetooth
  WI-FI      TCP to the Pi's IP on port 9999
  BLUETOOTH  RFCOMM serial to the paired Pi (no IP needed)

WHY BOTH
  Wi-Fi is faster but needs the hotspot up and the right IP.
  Bluetooth is slower but "just works" once paired — no IP to chase.
  AUTO gives you Wi-Fi when it's there and Bluetooth when it isn't.

PI SETUP
  Wi-Fi only:      nothing extra, it already listens on :9999
  Bluetooth too:   sudo apt-get install -y python3-bluez bluez
                   sudo bluetoothctl
                     power on
                     discoverable on
                     pairable on
                     agent on
                     default-agent
                   (then pair from the phone's Bluetooth settings)

  Run: sudo python3 motonav.py
  It prints:  TCP listening on :9999
              Bluetooth listening on RFCOMM channel 22
  The top bar shows WIFI or BT depending on which is carrying data.

APP SETUP
  1. Build the APK (Android Studio > Build > Build APK(s))
  2. Pick a CONNECTION mode (AUTO recommended)
  3. Wi-Fi: enter the Pi IP + port, tap SAVE ADDRESS
     Bluetooth: enter the Pi's Bluetooth name (default "raspberrypi")
  4. Enable Accessibility, grant Location (and Bluetooth on Android 12+)
  5. Start navigation in Google Maps

The status line shows CONNECTED VIA WI-FI or CONNECTED VIA BLUETOOTH.

THEME SWITCHER
  AUTO / DAY / NIGHT — controls the Pi display over whichever link is up.
  Re-sent automatically whenever the Pi reconnects.
