# Sensor profiling

Measures how commercial BLE wearables hold up under a real site's WiFi
(for example a clinic's therapy gym) before they are used with MEDWEAR:
whether the link stays up, how much data arrives, and, for sensors that
send their own sample timestamps, how old the samples are when they reach
the host. One **field session** is one visit to a site. The site's own WiFi
is the load; the tool generates none.

Every run records the BLE controller's HCI traffic (btmon), the
channel-hopping map (AFH) and connection RSSI every 15 s, a rosbag of the
driver's topics and the driver's log. A separate background monitor
records WiFi airtime on the forced channel, network counts and Bluetooth
crowding on its own radios. Afterwards, `analyze_session.py` derives every
metric from these raw captures.

Sensors are described by **profiles** (`sensors/<model>/`), so a new
sensor needs a YAML file and, optionally, a decoder for its own protocol;
see [Adding a sensor](#adding-a-sensor).

## What is measured

| For every sensor | For sensors whose decoder reads their sample clock |
|---|---|
| connection parameters (interval, latency, supervision timeout, MTU, PHY) | completeness of the timed stream (samples received / expected) |
| link losses by HCI reason, time to the first loss, reconnects and when data came back | added delay: host arrival minus the sensor's frame timestamp |
| connected share of the run | deadline misses M(D): share of samples later than D or never delivered |
| notifications and payload bytes per second (delivery) | end-to-end sample age (added delay plus the sensor's framing) |
| AFH channels in use, and inside the forced WiFi channel | runaway backlog (delay that grows and never recovers) |
| RX inter-arrival jitter, encryption, BlueZ stack faults | the sensor's own counters (e.g. Polar ACC/ECG/HR completeness, Corsano file protocol) |

The run design per session: `free_channel` (AFH free to choose its
channels) and `forced_k0/k2/k8` (the BLE data channels inside the site's
busiest WiFi channel, plus k of the quietest other channels), each once per
block, in randomised blocks, one sensor at a time.

## Hardware

- A Linux laptop whose Bluetooth controller (`btmon_controller`, usually
  `hci0`) is the central under test. Results describe this controller's
  channel selection, not the sensor's: in BLE the central sets the map.
- A USB WiFi adapter that supports monitor mode, physically separate from
  the laptop's built-in radio (`background_monitor.wifi_iface`; tested: TP-Link
  Archer T3U Plus, rtw88_8822bu). The built-in WiFi shares the Bluetooth
  antenna on most laptops; the code refuses to use it and switches it off
  during a session.
- An nRF52840 dongle with the nRF Sniffer for Bluetooth LE firmware
  (`background_monitor.ble_sniffer_port`), used for advertiser counts only.
- The sensors, each with a ROS 2 driver that connects and streams while it
  runs (the examples use the ros4healthcare drivers `polar_ros2` and
  `corsano_ros`).

## Setup

```bash
# ROS 2 (Jazzy) and the sensors' drivers, built in a workspace
source /opt/ros/jazzy/setup.bash
export ROS_WS=~/ros2_ws && source $ROS_WS/install/setup.bash

# system tools: BlueZ (btmon, bluetoothctl, hcitool), Wireshark (tshark,
# dumpcap), iw, rfkill, nmcli; the nRF sniffer tools (nrfutil ble-sniffer)
sudo apt install bluez tshark iw rfkill network-manager

pip install -r sensor_profiling/requirements.txt

# one-time permissions: channel changes without sudo, the sniffer's serial port
sudo setcap cap_net_admin+eip "$(which iw)"
sudo usermod -aG dialout "$USER"
```

Then, in `config.yaml`:

1. list your sensor units under `sensors:` (profile, name, Bluetooth address);
2. set `background_monitor.wifi_iface` (`iw dev`) and `ble_sniffer_port`;
3. check the session design under `profile_session:` (run length, blocks, k values).

Before the first session, dry-run each sensor's driver (its `launch_cmd`)
and check that data streams, and run the monitor's 10 s self-test:
`python3 background_monitor.py check`.

## A field session

Run everything from `sensor_profiling/`:

```bash
python3 background_monitor.py check     # once: adapter and sniffer work (prints only)
python3 profile_session.py run          # survey (~7 min), monitor, 24 runs (~2.5 h), parse
python3 profile_session.py run --resume # after any interruption
python3 profile_session.py people 12    # log a people count, any time, from another terminal
python3 profile_session.py parse        # re-parse and re-analyse a session
```

`run` asks for the sudo password once (monitor mode). It then:

1. surveys all 2.4 GHz channels and picks the busiest by airtime as the
   forced channel, and the quietest ones as the extra channels for k > 0;
2. starts the background monitor as a child process on its own radios;
3. checks each sensor is advertising, then runs the schedule unattended.
   Before each run it reads the sensor's advertising RSSI and logs a warning
   if it moved by more than `rssi_tolerance_db` from its reference (the run
   starts anyway);
4. parses the captures into `results.csv`, joins the background monitor's
   time series onto each run (`results_with_background.csv`) and runs the
   analysis.

Sessions are folders `captures/field_<date>_<time>/`. Data is never
overwritten: files of an interrupted or re-run attempt are moved aside, and
`results.csv` keeps every attempt. `--test-mode` is a desk dry run that
leaves the internal WiFi on and doesn't insist on airtime data; never use
it for real data.

**Pass/fail.** Baseline runs (`free_channel`) fail on any unexpected
disconnect, on missing messages on a sensor's `expected_topics`, or when the
decoder's baseline check finds no new sensor data. Forced-channel runs
under load fail only if the sensor never connected: disconnects there are
data. Any run that doesn't force channels fails if its AFH map still shows
an earlier run's forced pattern.

**Before running in a clinical space:** get the facility's permission and
let the staff know. The stand-mounted runs collect no patient data; confirm
with the facility whether an ethics review is needed. Wearing a sensor on a
person is a separate study and needs ethics clearance.

## Results

```bash
python3 analyze_session.py captures/field_<date>_<time>             # one session
python3 analyze_session.py captures/field_* --pool                  # several site visits
```

Output in `analysis/<session>/`:

| File | Content |
|---|---|
| `tables/per_run.csv` | **the table to analyse**: one row per run, every metric, plus the site covariates (`bg_*` airtime, Bluetooth crowding, networks, people present, the forced mask and its extras' airtime, pre-run RSSI) |
| `tables/per_condition.csv` | per sensor and condition: ranges, delay percentiles, M(D), runaways |
| `tables/connections.csv`, `losses.csv` | every connection and every link loss, with recovery times |
| `tables/link_params.csv`, `sanity.csv` | negotiated link parameters; the sanity checks |
| `report.md` | headline numbers, sanity checks, excluded runs |
| `figures/` | delivery, channel maps, request/response, and for timed streams the delay CCDF, deadline-miss curves and per-run delay timelines |

`--pool` adds `analysis/pooled_<ids>/pooled_per_condition.csv`: per sensor
and condition across sessions, with Wilson 95 % CIs for the share of runs
with a link loss and for runaways.

Every metric is computed from the raw captures; `results.csv` (written
during the session) is only cross-checked. Definitions are in
`analyze_session.py`'s header.

## Adding a sensor

A profile is a folder in `sensors/`:

```
sensors/<model>/
  sensor.yaml   driver command, topics, battery topic (required)
  decoder.py    the sensor's own protocol (optional)
```

1. Copy `sensors/_template/` to `sensors/<model>/` and fill in
   `sensor.yaml`: the driver's `launch_cmd` (with `{mac_address}`), its
   `expected_topics` (checked in baseline runs) and `advisory_topics`
   (warning only).
2. List the unit in `config.yaml` under `sensors:`. Without `decoder.py`
   this is enough: every link metric above works for any BLE sensor.
3. For metrics only the protocol can give, write `decoder.py`
   (interface: `session_lib/sensors.py`). Every hook is optional:
   - `NOTIFICATIONS` + `metrics(ctx)` + `COLUMNS`: the sensor's own
     numbers from its notifications (`results.csv` columns), e.g.
     completeness from sequence numbers;
   - `summary_lines(parsed)`: lines in the session log;
   - `baseline_check(parsed)`: fail a baseline run in which the sensor
     recorded nothing new;
   - `TIMED_STREAM` + `timed_frame(value, length)`: for sensors whose
     notifications carry their sample clock; this unlocks added delay, M(D)
     and runaway detection in the analysis.

   The template decoder counts standard Heart Rate Measurement
   notifications. `sensors/polar_h10/` (timed ECG frames, PMD protocol)
   and `sensors/corsano_287_2b/` (a polled file protocol) are complete
   examples.

`python3 -m pytest tests/` (from `sensor_profiling/`) checks that every
profile loads and that each decoder returns the columns it declares.

To find a characteristic's notifications in a capture:
`btmon -r <run>.btsnoop | grep -A3 "Handle Value Notification"`. On a
reconnect to a bonded sensor BlueZ uses its cached GATT table and btmon shows
no UUIDs, so give the characteristic's ATT handle too (`att_handle`).

## Privacy

Nearby people's devices (GDPR / Swiss FADP) are handled like this:

- **Background monitor:** Bluetooth advertiser addresses and WiFi BSSIDs are
  held in memory only, to count distinct ones; only counts are written. The
  sniffer streams through a FIFO, no pcap is kept.
- **Sensor runs:** while a driver looks for its sensor, BlueZ scans, so btmon
  briefly records every advertiser the laptop hears, and some drivers print
  scanned devices to their log. `privacy_scrub.py` runs after every run: it
  filters each capture to your own devices, checks the parsed metrics are
  identical and btmon shows no foreign address, then replaces the raw file;
  device lists in driver logs become a count.
- `session.log` keeps DEBUG lines only from the tool's own loggers.
- `people.csv` holds headcounts only.

## Limitations

- Results describe the laptop's Bluetooth controller (the central chooses
  the channel map); a phone or a hospital gateway may behave differently.
- Airtime is a lower bound: frames the adapter can't decode, non-WiFi energy
  and backoff gaps aren't counted.
- The nRF sniffer covers the advertising channels only; it can't follow the
  data connection.
- With stand-mounted sensors, physiological values (ECG, HR) are not
  meaningful; only link and delivery metrics are.
- Three blocks per session are few; plan several visits and report
  intervals.

## Origin

The tool generalises the field-session code of the MEDWEAR paper's wireless
experiments ([SCAI-Lab/wireless_experiments](https://github.com/SCAI-Lab/wireless_experiments)),
which also holds the chamber experiments and the paper's figures. Sessions
recorded with it (`gym_<date>` folders, rows `gym_free`/`gym_forced`) are
found, parsed and analysed by this version; their family labels in the
analysis are `field_free`/`field_forced`.
