# /**************************************************************************/
# /*!           mmt_parse_wiff.py File Information
# ** =========================================================================
# **
# **
# **  $Author: Ali Marjovi $
# **  $Date: Oct 2021 $
# **
# **  $Id: 1$
# **  $Revision: 2$
# **
# **  $Updated: Oct 29, 2021 $
# **  Summary: Parsing the binary files of PPG, Bioz and ECG (as input) and
# **           generating csv files (as output)
# **
# ** =========================================================================
# ** Copyright (c) 2021 MMT All rights reserved.
# **
# ** =========================================================================
# **
# **/

# # python Corsano_parse_wiff.py "<path/to/raw/data>" --japanTimezone --isManualData


print("Importing packages..")
import pandas as pd
import os, sys
import shutil
import zipfile
from pathlib import Path
import numpy as np
from scipy.signal import butter, filtfilt
from datetime import datetime, timedelta, timezone
import math
import traceback
# import constants
from parselib.parsers.ParserCode import constants
import argparse
import time
from dateutil import tz

# try:
#     from zoneinfo import ZoneInfo  # Python 3.9+
# except Exception:  # pragma: no cover
#     try:
#         from backports.zoneinfo import ZoneInfo  # Python <3.9 fallback
#     except Exception:
#         ZoneInfo = None  # Fallback if neither is available

global bioz_time
global ecg_time

ppg_times = np.zeros(8 * 2, dtype=np.int64)
bioz_time = 0
acc_time = 0
ecg_time = 0
BIOZ_SR = 25

open_files = []
file_names = []
bioz_outputfile = None
acc_outputfile = None
input_file_name = None
file_path = None
output_path = None
time_zone = timezone.utc


def is_hidden_file(filepath):
    """Check if the file is a hidden file (starts with ._ or .)"""
    filename = os.path.basename(filepath)
    return filename.startswith('._') or filename.startswith('.')


def metric_is_file_header(metric):
    if metric == 0x0A:
        return True
    return False


def metric_is_device_info_header(metric):
    if metric == 0x0B:
        return True
    return False


def metric_is_device_measurement_header(metric):
    if metric == 0x0C:
        return True
    return False


def metric_is_ppg(metric):
    if metric > 120 and metric < 128:
        return True
    return False


def metric_is_ecg(metric):
    if metric == 100:
        return True
    return False


def metric_is_bioz(metric):
    if metric == 61:
        return True
    return False


def metric_is_time(metric):
    if metric == 0x1E:
        return True
    return False


def metric_is_acc(metric):
    if metric == 0x2B:
        return True
    return False


def butter_lowpass_filter(data, cutoff, fs, order):
    nyq = 0.5 * fs  # Nyquist Frequency
    normal_cutoff = cutoff / nyq
    # Get the filter coefficients
    b, a = butter(order, normal_cutoff, btype="low", analog=False)
    y = filtfilt(b, a, data)
    return y


def filter_ecg():
    file_name = (
            file_path
            + constants.folder_delimiter
            + "MMT_ECG_"
            + input_file_name
            + hex(0x64)
            + ".csv"
    )
    ecg_table = pd.read_csv(file_name, header=0)

    filtered_ecg = butter_lowpass_filter(ecg_table["ecg"], 30, 256, 2)
    ecg_table.insert(6, "filted_ecg", filtered_ecg, True)
    file_name2 = (
            file_path
            + constants.folder_delimiter
            + "MMT_ECG_"
            + input_file_name
            + hex(0x64)
            + "_filtered"
            + ".csv"
    )
    ecg_table.to_csv(file_name2)


def save_ecg_to_file(metric_id, chunk_index, time, ms, values, ecgs):
    file_name = (
            file_path
            + constants.folder_delimiter
            + "MMT_ECG_"
            + input_file_name
            + hex(metric_id)
            + ".csv"
    )
    write_header = False
    FILE = Path(file_name)
    if not FILE.is_file():
        write_header = True

    myfile = open(file_name, "a")
    if write_header:
        myfile.write("timestamp,date,ms,metric_id,chunk_index,value,ecg\n")
    index = 0
    for value in values:
        myfile.write(
            str(time)
            + ","
            + str(ms)
            + ","
            + str(hex(metric_id))
            + ","
            + str(chunk_index)
            + ","
            + str(value)
            + ","
            + str(ecgs[index])
            + "\n"
        )
        index += 1
    myfile.close()


def get_bioz_file(metric_id):
    global bioz_outputfile

    if bioz_outputfile is None:
        file_name = (
                output_path
                + constants.folder_delimiter
                + "bioz"
                + constants.folder_delimiter
                + "MMT_BioZ_"
                + input_file_name
                + hex(metric_id)
                + ".csv"
        )
        FILE = Path(file_name)
        if not FILE.is_file():
            # New file: add the header
            bioz_outputfile = open(file_name, "a")
            bioz_outputfile.write(
                "timestamp,date, metric_id, chunk_index,value, BioZ \n"
            )
        else:
            # Existing file: just open it to add new data at the end
            bioz_outputfile = open(file_name, "a")

    return bioz_outputfile


def save_bioz_to_file(b_time, metric_id, chunk_index, values, bioz):
    biozfile_optim = True

    if not biozfile_optim:
        file_name = (
                output_path
                + constants.folder_delimiter
                + "bioz"
                + constants.folder_delimiter
                + "MMT_BioZ_"
                + input_file_name
                + hex(metric_id)
                + ".csv"
        )
        write_header = False
        FILE = Path(file_name)
        if not FILE.is_file():
            write_header = True

        myfile = open(file_name, "a")
        if write_header:
            myfile.write("timestamp,date, metric_id, chunk_index,value, BioZ \n")
    else:
        myfile = get_bioz_file(metric_id)

    index = 0
    for index in range(values.size):
        dt = datetime.fromtimestamp(b_time / 1000, tz=timezone.utc)
        dt = dt.astimezone(time_zone)
        # ISO 8601 with milliseconds
        dt_str = dt.isoformat(timespec="milliseconds")
        myfile.write(
            str(b_time)
            + ","
            + dt_str
            + ","
            + str(hex(metric_id))
            + ","
            + str(chunk_index)
            + ","
            + str(values[index])
            + ","
            + str(bioz[index])
            + "\n"
        )
        b_time = b_time + 1000 / BIOZ_SR

    if not biozfile_optim:
        myfile.close()


def get_acc_file(metric_id):
    global acc_outputfile

    if acc_outputfile is None:
        file_name = (
                output_path
                + constants.folder_delimiter
                + "acc"
                + constants.folder_delimiter
                + "MMT_ACC1_"
                + input_file_name
                + hex(metric_id)
                + ".csv"
        )
        FILE = Path(file_name)
        if not FILE.is_file():
            # New file: add the header
            acc_outputfile = open(file_name, "a")
            acc_outputfile.write(
                "timestamp,date,metric_id,chunk_index,quality,body_pose,accX,accY,accZ \n"
            )
        else:
            # Existing file: just open it to add new data at the end
            acc_outputfile = open(file_name, "a")

    return acc_outputfile


def save_acc_to_file(
        time, metric_id, chunk_index, quality, body_pose, accX, accY, accZ
):
    accfile_optim = True

    if not accfile_optim:
        file_name = (
                output_path
                + constants.folder_delimiter
                + "acc"
                + constants.folder_delimiter
                + "MMT_ACC1_"
                + input_file_name
                + hex(metric_id)
                + ".csv"
        )
        write_header = False
        FILE = Path(file_name)
        if not FILE.is_file():
            write_header = True
        myfile = open(file_name, "a")
        if write_header:
            myfile.write(
                "timestamp,date,metric_id,chunk_index,quality,body_pose,accX,accY,accZ \n"
            )
    else:
        myfile = get_acc_file(metric_id)

    for index in range(32):
        dt = datetime.fromtimestamp(time / 1000, tz=timezone.utc)
        dt = dt.astimezone(time_zone)
        # ISO 8601 with milliseconds
        dt_str = dt.isoformat(timespec="milliseconds")
        myfile.write(
            str(time)
            + ","
            + dt_str
            + ","
            + str(hex(metric_id))
            + ","
            + str(chunk_index)
            + ","
            + str(quality)
            + ","
            + str(body_pose)
            + ","
            + str(accX[index])
            + ","
            + str(accY[index])
            + ","
            + str(accZ[index])
            + "\n"
        )
        time = time + 1000 / 32

    if not accfile_optim:
        myfile.close()


def get_ppg_file(metric_id, led_pd_pos):
    file_id = "" + hex(metric_id) + hex(led_pd_pos)
    file_opened = False
    try:
        index = file_names.index(file_id)
        if index >= 0:
            file_opened = True
            file = open_files[index]
            # print('Found file at index ', index, file_id)
        else:
            file_opened = False
    except:
        file_opened = False

    if not file_opened:
        Path(output_path).mkdir(parents=True, exist_ok=True)
        file_name = (
                output_path
                + constants.folder_delimiter
                + "ppg"
                + constants.folder_delimiter
                + "MMT_PPG_"
                + input_file_name
                + file_id
                + ".csv"
        )
        FILE = Path(file_name)
        if not FILE.is_file():
            # New file: add the header
            file = open(file_name, "a")
            file.write(
                "timestamp,date,metric_id,chunk_index,quality,body_pose,led_pd_pos,offset,exp,led,gain,value \n"
            )
        else:
            # Existing file: just open it to add new data at the end
            file = open(file_name, "a")
        open_files.append(file)
        file_names.append(file_id)

    return file


last_value = [0] * 12


def save_ppg_to_file(
        time,
        metric_id,
        chunk_index,
        quality,
        body_pose,
        led_pd_pos,
        offset,
        exp,
        led1,
        led2,
        led3,
        led4,
        gain1,
        gain2,
        gain3,
        gain4,
        values,
        SR,
):
    ppgfile_optim = True

    if not ppgfile_optim:
        Path(output_path).mkdir(parents=True, exist_ok=True)
        file_name = (
                output_path
                + constants.folder_delimiter
                + "ppg"
                + constants.folder_delimiter
                + "MMT_PPG_"
                + input_file_name
                + hex(metric_id)
                + hex(led_pd_pos)
                + ".csv"
        )
        write_header = False
        FILE = Path(file_name)
        if not FILE.is_file():
            write_header = True

        myfile = open(file_name, "a")
        if write_header:
            myfile.write(
                "timestamp,date,metric_id,chunk_index,quality,body_pose,led_pd_pos,offset,exp,led,gain,value \n"
            )

    index = 0
    for value in values:
        if index < SR / 4:
            led = led1
            gain = gain1
        else:
            if index < SR / 2:
                led = led2
                gain = gain2
            else:
                if index < 3 * SR / 4:
                    led = led3
                    gain = gain3
                else:
                    led = led4
                    gain = gain4
        index += 1
        if ppgfile_optim:
            myfile = get_ppg_file(metric_id, led_pd_pos)

        value = value + gain * 32768 - 16394  # PPG 19 bits
        if (
                last_value[metric_id - 120] > 0
                and value > last_value[metric_id - 120] + 16394
        ):
            value = value - 32768
        else:
            if (
                    last_value[metric_id - 120] > 0
                    and value < last_value[metric_id - 120] - 16394
            ):
                value = value + 32768

        last_value[metric_id - 120] = value
        dt = datetime.fromtimestamp(time / 1000, tz=timezone.utc)
        dt = dt.astimezone(time_zone)
        # ISO 8601 with milliseconds
        dt_str = dt.isoformat(timespec="milliseconds")
        myfile.write(
            str(time)
            + ","
            + dt_str
            + ","
            + str(hex(metric_id))
            + ","
            + str(chunk_index)
            + ","
            + str(quality)
            + ","
            + str(body_pose)
            + ","
            + str(led_pd_pos)
            + ","
            + str(offset)
            + ","
            + str(exp)
            + ","
            + str(led)
            + ","
            + str(2)
            + ","
            + str(value)
            + "\n"
        )
        time = time + 1000 / SR

    if not ppgfile_optim:
        myfile.close()


def process_ppg_data(metric_array, processed_index, metric_id):
    chunk_index = int(metric_array[processed_index])
    processed_index += 1
    quality = int(metric_array[processed_index])
    processed_index += 1
    body_pose = int(metric_array[processed_index])
    processed_index += 1
    data_format = int(metric_array[processed_index])
    processed_index += 1
    SR = 32
    if data_format == 0x60:
        SR = 32
    else:
        if data_format == 0x61:
            SR = 64
        else:
            if data_format == 0x62:
                SR = 128
            else:
                if data_format == 0x63:
                    SR = 256
                else:
                    if data_format == 0x64:
                        SR = 512
                    else:
                        print("PPG error: format not supported", data_format)
                        SR = 0
                        return  # processed_index
    led_pd_pos = int(metric_array[processed_index])
    processed_index += 1
    offset = int(metric_array[processed_index])
    processed_index += 1
    exp = int(metric_array[processed_index])
    processed_index += 1
    led1 = int(metric_array[processed_index])
    processed_index += 1
    led2 = int(metric_array[processed_index])
    processed_index += 1
    led3 = int(metric_array[processed_index])
    processed_index += 1
    led4 = int(metric_array[processed_index])
    processed_index += 1

    gain1 = int(metric_array[processed_index])
    processed_index += 1
    gain2 = int(metric_array[processed_index])
    processed_index += 1
    gain3 = int(metric_array[processed_index])
    processed_index += 1
    gain4 = int(metric_array[processed_index])
    processed_index += 1

    values = np.empty(SR, dtype=object)
    for i in range(0, SR):
        value = metric_array[processed_index + 1] * 256 + metric_array[processed_index]
        processed_index += 2
        values[i] = value

    channel_offset = 0
    lp = led_pd_pos & 0x0F
    if lp == 0xC:
        channel_offset = 1

    save_ppg_to_file(
        ppg_times[(metric_id - 120) * 2 + channel_offset],
        metric_id,
        chunk_index,
        quality,
        body_pose,
        led_pd_pos,
        offset,
        exp,
        led1,
        led2,
        led3,
        led4,
        gain1,
        gain2,
        gain3,
        gain4,
        values,
        SR,
    )

    ppg_times[(metric_id - 120) * 2 + channel_offset] = (
            ppg_times[(metric_id - 120) * 2 + channel_offset] + 1000
    )

    return processed_index


def process_acc_data(metric_array, processed_index, metric_id):
    global acc_time
    chunk_index = int(metric_array[processed_index])
    processed_index += 1
    quality = int(metric_array[processed_index])
    processed_index += 1
    body_pose = int(metric_array[processed_index])
    processed_index += 1
    data_format = int(metric_array[processed_index])
    if data_format != 0x6E:
        print("ACC error: format not supported", data_format)
    processed_index += 1

    accX = np.empty(32, dtype=object)
    accY = np.empty(32, dtype=object)
    accZ = np.empty(32, dtype=object)
    for i in range(32):
        value = metric_array[processed_index + 1] << 8 | metric_array[processed_index]
        if value > 0x7FFF:
            value = value - 0x10000
        accX[i] = value
        processed_index += 2
        value = metric_array[processed_index + 1] << 8 | metric_array[processed_index]
        if value > 0x7FFF:
            value = value - 0x10000
        accY[i] = value
        processed_index += 2
        value = metric_array[processed_index + 1] << 8 | metric_array[processed_index]
        if value > 0x7FFF:
            value = value - 0x10000
        accZ[i] = value
        processed_index += 2

    save_acc_to_file(
        acc_time, metric_id, chunk_index, quality, body_pose, accX, accY, accZ
    )
    acc_time = acc_time + 1000  # because it is every seconds
    return processed_index


def process_time_data(metric_array, processed_index, metric_id):
    global acc_time
    global bioz_time
    chunk_index = int(metric_array[processed_index])
    processed_index += 1
    quality = int(metric_array[processed_index])
    processed_index += 1
    t = int(metric_array[processed_index])
    processed_index += 1
    t = int(metric_array[processed_index]) << 8 | t
    processed_index += 1
    t = int(metric_array[processed_index]) << 16 | t
    processed_index += 1
    t = int(metric_array[processed_index]) << 24 | t
    processed_index += 1
    # print("Time: ", t)
    if (acc_time > t * 1000 + 1000) or (acc_time < t * 1000 - 1000):
        acc_time = t * 1000
    for i in range(len(ppg_times)):
        if (ppg_times[i] > t * 1000 + 6000) or (ppg_times[i] < t * 1000 - 6000):
            if i == 12:
                tt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
                print("\ntime diff ", t * 1000 - ppg_times[i], "  T: ", tt)
            ppg_times[i] = t * 1000

    if (bioz_time > t * 1000 + 6000) or (bioz_time < t * 1000 - 6000):
        bioz_time = t * 1000
    return processed_index


def process_ecg_data(metric_array, processed_index, metric_id):
    chunk_index = int(metric_array[processed_index])
    processed_index += 1

    quality = int(metric_array[processed_index])
    processed_index += 1
    body_pose = int(metric_array[processed_index])
    processed_index += 1
    data_format = int(metric_array[processed_index])
    processed_index += 1
    if data_format != 0x1:
        print("ECG error: format not supported", data_format)

    streams = int(metric_array[processed_index])
    processed_index += 1

    time1 = int(metric_array[processed_index])
    processed_index += 1
    time1 = int(metric_array[processed_index]) * 256 + time1
    processed_index += 1
    time1 = int(metric_array[processed_index]) * 256 * 256 + time1
    processed_index += 1
    time1 = int(metric_array[processed_index]) * 256 * 256 * 256 + time1
    processed_index += 1
    ms = int(metric_array[processed_index])
    processed_index += 1
    ms = int(metric_array[processed_index]) * 256 + ms
    processed_index += 1

    values = np.empty(streams, dtype=object)
    ecgs = np.empty(streams, dtype=object)
    prev_value = 0
    for i in range(0, streams):
        value = (
                metric_array[processed_index + 2] * 256 * 256
                + metric_array[processed_index + 1] * 256
                + metric_array[processed_index]
        )
        if value > 0x3FFFFF:
            value = prev_value
        processed_index += 3
        values[i] = value
        prev_value = value

        if value > 0x20000:
            value = (-1) * (0x3FFFF - value)
        value = value * (-1)

        if value > 0x20000:
            value = (-1) * (0x3FFFF - value)

        # converting ecg to microvolt.
        ecgs[i] = int(value * 1000 / (0x20000) / 15 * 1000)

    save_ecg_to_file(metric_id, chunk_index, time1, ms, values, ecgs)
    return processed_index


def process_bioz_data(metric_array, processed_index, metric_id, metric_size):
    global bioz_time
    chunk_index = int(metric_array[processed_index])
    processed_index += 1
    quality = int(metric_array[processed_index])
    processed_index += 1
    body_pose = int(metric_array[processed_index])
    processed_index += 1
    data_format = int(metric_array[processed_index])
    processed_index += 1
    if data_format != 0x1:
        print("BioZ error: format not supported", data_format)

    no_samples = math.floor((metric_size - 4) / 3)

    values = np.empty(no_samples, dtype=object)
    bioz = np.empty(no_samples, dtype=object)
    for i in range(0, no_samples):
        value = (
                metric_array[processed_index + 2] * 256 * 256
                + metric_array[processed_index + 1] * 256
                + metric_array[processed_index]
        )
        processed_index += 3
        values[i] = value
        if value >= 0x80000:
            value = 0x80000 - value
        bioz[i] = ((value / 0x80000) + 1.0) / 2.0

    save_bioz_to_file(bioz_time, metric_id, chunk_index, values, bioz)
    bioz_time = bioz_time + no_samples * 1000.0 / BIOZ_SR
    return processed_index


def process_bioz_adc_data(metric_array, processed_index, metric_id, metric_size):
    chunk_index = int(metric_array[processed_index])
    processed_index += 1
    quality = int(metric_array[processed_index])
    processed_index += 1
    body_pose = int(metric_array[processed_index])
    processed_index += 1
    data_format = int(metric_array[processed_index])
    processed_index += 1
    if data_format != 0x1:
        print("BioZ error format not supported. ", data_format)

    no_samples = math.floor((metric_size - 4) / 6)
    print("Nr of samples: ", no_samples)

    values = np.empty(no_samples, dtype=object)
    adcvalues = np.empty(no_samples, dtype=object)
    bioz = np.empty(no_samples, dtype=object)
    for i in range(0, no_samples):
        value = (
                metric_array[processed_index + 2] * 256 * 256
                + metric_array[processed_index + 1] * 256
                + metric_array[processed_index]
        )
        valbytes = [
            metric_array[processed_index],
            metric_array[processed_index + 1],
            metric_array[processed_index + 2],
        ]
        value2 = int.from_bytes(bytearray(valbytes), "little", signed=False)
        processed_index += 3
        values[i] = value2
        adcvalue = (
                metric_array[processed_index + 2] * 256 * 256
                + metric_array[processed_index + 1] * 256
                + metric_array[processed_index]
        )
        adcbytes = [
            metric_array[processed_index],
            metric_array[processed_index + 1],
            metric_array[processed_index + 2],
        ]
        adcvalue2 = int.from_bytes(bytearray(adcbytes), "little", signed=True)
        processed_index += 3
        adcvalues[i] = adcvalue2
        if value2 > 100000:
            print(" bV ")
        if value >= 0x80000:
            value = 0x80000 - value
        bioz[i] = ((value / 0x80000) + 1.0) / 2.0

    save_bioz_to_file(bioz_time, metric_id, chunk_index, values, adcvalues)
    return processed_index


def process_file_header(metric_array):
    global acc_time
    global bioz_time
    packet_size = len(metric_array)
    processed_index = 0

    processed_index += 12

    t = int(metric_array[processed_index])
    processed_index += 1
    t = int(metric_array[processed_index]) << 8 | t
    processed_index += 1
    t = int(metric_array[processed_index]) << 16 | t
    processed_index += 1
    t = int(metric_array[processed_index]) << 24 | t
    tt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    print("the time of the header is ", tt)

    if (acc_time > t * 1000 + 6000) or (acc_time < t * 1000 - 6000):
        acc_time = t * 1000
    for i in range(len(ppg_times)):
        if (ppg_times[i] > t * 1000 + 6000) or (ppg_times[i] < t * 1000 - 6000):
            if i == 12:
                print("header time diff  ", t * 1000 - ppg_times[i])
            ppg_times[i] = t * 1000

    if (bioz_time > t * 1000 + 6000) or (bioz_time < t * 1000 - 6000):
        bioz_time = t * 1000

    processed_index += 1
    if packet_size != processed_index:
        print("Warning: measurement info packet is corrputed")
    return processed_index


def process_device_info(metric_array):
    packet_size = len(metric_array)
    processed_index = 0

    processed_index += 7
    FW_v1 = metric_array[processed_index]
    processed_index += 1
    FW_v2 = metric_array[processed_index]
    processed_index += 1
    FW_v3 = metric_array[processed_index]
    processed_index += 1
    FW_v4 = metric_array[processed_index]

    print("FW version is :", FW_v1, FW_v2, FW_v3, FW_v4)
    processed_index += 1

    device_name = metric_array[processed_index: processed_index + 12]
    print("device name is :", device_name)

    processed_index += 12

    processed_index += 2

    if packet_size != processed_index:
        print("Warning: measurement info packet is corrputed")
    return processed_index


def process_measurement_info(metric_array):
    packet_size = len(metric_array)
    processed_index = 0

    processed_index += 3
    HW_ID0 = metric_array[processed_index]
    processed_index += 1
    HW_ID1 = metric_array[processed_index]
    processed_index += 1
    MID0 = metric_array[processed_index]
    processed_index += 1
    MID1 = metric_array[processed_index]

    processed_index += 1

    serial_number = metric_array[processed_index: processed_index + 8]
    processed_index += 8

    processed_index += 4

    ff = metric_array[processed_index]
    processed_index += 1
    if ff != 0xFF:
        print("Error: measurement info packet is corrputed.")
        processed_index += 1
    else:
        zero = metric_array[processed_index]
        processed_index += 1
        if zero != 0:
            print("Error: measurement info packet is corrputed.")

        else:
            start_stop = metric_array[processed_index]
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ppg_times[0] / 1000))

    processed_index += 1

    if packet_size != processed_index:
        print("Warning: measurement info packet is corrputed")

    return processed_index


def process_metric_packet(metric_array):
    packet_size = len(metric_array)
    processed_index = 0
    while processed_index < packet_size:
        metric_id = metric_array[processed_index]
        processed_index += 1
        metric_size = int(metric_array[processed_index + 1]) * 256 + int(
            metric_array[processed_index]
        )
        processed_index += 2
        if metric_is_ppg(metric_id):
            processed_index = process_ppg_data(metric_array, processed_index, metric_id)
        else:
            if metric_is_ecg(metric_id):
                processed_index = process_ecg_data(
                    metric_array, processed_index, metric_id
                )
            else:
                if metric_is_bioz(metric_id):
                    if metric_size >= 154:
                        processed_index = process_bioz_adc_data(
                            metric_array, processed_index, metric_id, metric_size
                        )
                    else:
                        processed_index = process_bioz_data(
                            metric_array, processed_index, metric_id, metric_size
                        )
                else:
                    if metric_is_time(metric_id):
                        processed_index = process_time_data(
                            metric_array, processed_index, metric_id
                        )
                    else:
                        if metric_is_acc(metric_id):
                            processed_index = process_acc_data(
                                metric_array, processed_index, metric_id
                            )
                        else:
                            print(
                                ">>>>>>>>>> metric "
                                + hex(metric_id)
                                + " not supported <<<<<<<<<<, index: ",
                                processed_index,
                            )
                            return


def parse_wiff_file():
    global acc_outputfile
    global bioz_outputfile
    global outputfile
    print("Parsing file:")
    print(input_file_name)
    print(" with path: ")
    print(file_path)
    file_name = file_path + input_file_name + ".wiff"
    print(file_name)
    FILE = Path(file_name)
    packet_index = 0
    read_bytes = 0
    file_names.clear()
    open_files.clear()
    if FILE.is_file():
        file = open(file_name, "rb")
        preamble = file.read(3)
        read_bytes = read_bytes + 3
        if len(preamble) != 3:
            print("Preamble error at the begining of the file.")
            file.close()
            return

        next_byte = b"R"
        while preamble:
            while preamble != b"OHR" and next_byte:
                next_byte = file.read(1)
                read_bytes = read_bytes + 1
                preamble = preamble[1:3] + next_byte

            if next_byte != b"R":
                print("ERROR could not find preamble ", preamble, next_byte, read_bytes)
                break
            packet_index = packet_index + 1
            try:
                size_bytes = file.read(2)
                read_bytes = read_bytes + 2
                packet_size = int(size_bytes[1]) * 256 + int(size_bytes[0])
                if packet_size < 4000 and packet_size > 2:
                    packet = file.read(packet_size)
                    if not packet:
                        print("ERROR could not read packet ", packet_size)
                        break
                    read_bytes = read_bytes + len(packet)
                    if len(packet) < packet_size:
                        print(">>> Incomplete packet: ", len(packet), "/", packet_size)
                        break
                    if packet[0] == 0x0F:
                        process_metric_packet(packet[1:])
                    else:
                        if metric_is_file_header(packet[0]):
                            processed_index = process_file_header(packet[1:])
                        else:
                            if metric_is_device_info_header(packet[0]):
                                processed_index = process_device_info(packet[1:])
                            else:
                                if metric_is_device_measurement_header(packet[0]):
                                    processed_index = process_measurement_info(
                                        packet[1:]
                                    )
                                else:
                                    print("Packet error: " + packet[0])

                preamble = file.read(3)
                read_bytes = read_bytes + 3
            except Exception as e:
                if hasattr(e, "message"):
                    print(e.message)
                else:
                    print("EXCEPTION reading file !!!", type(e))
                traceback.print_exc()
                break
        print(read_bytes, "bytes read")
    else:
        return False
    file.close()
    for outputfile in open_files:
        outputfile.close()
        outputfile = None
    if bioz_outputfile is not None:
        bioz_outputfile.close()
        bioz_outputfile = None
    if acc_outputfile is not None:
        acc_outputfile.close()
        acc_outputfile = None
    return


def parse_data():
    global input_file_name
    # Get all the files in the file_path directory
    wiff_files = sorted(
        [f for f in Path(file_path).glob("*.wiff") if not is_hidden_file(str(f))],
        key=lambda f: f.name
    )

    print("\n \n Parsing data " + file_path + "\n")

    # 隠しファイルを除外
    skipped_count = len([f for f in Path(file_path).glob("*.wiff") if is_hidden_file(str(f))])
    if skipped_count > 0:
        print(f"Skipping {skipped_count} hidden files starting with '._ or .'")

    for idx, file in enumerate(wiff_files, 1):
        input_file_name = str(file.name).split(".")[0]
        print("=" * 60)
        print(f"[{idx}/{len(wiff_files)}] Processing file: {file.name}")
        print("-" * 60)
        print("→ Parsing data...")
        start = time.perf_counter()
        data = parse_wiff_file()
        stop = time.perf_counter()
        print("Processing time: ", stop - start, "sec")
        print(f"✔ Done with {input_file_name}.")


def flatten_folders(parent_folder):
    for lvl1 in os.listdir(parent_folder):
        # 隠しファイルをスキップ
        if is_hidden_file(lvl1):
            continue

        lvl1_path = os.path.join(parent_folder, lvl1)

        if os.path.isdir(lvl1_path):
            # Loop through items inside lvl1
            for item in os.listdir(lvl1_path):
                # 隠しファイルをスキップ
                if is_hidden_file(item):
                    continue

                item_path = os.path.join(lvl1_path, item)

                if os.path.isdir(item_path):
                    # lvl2 is a folder → move its files
                    for file in os.listdir(item_path):
                        # 隠しファイルをスキップ
                        if is_hidden_file(file):
                            continue

                        file_path = os.path.join(item_path, file)
                        if os.path.isfile(file_path):
                            shutil.move(file_path, os.path.join(parent_folder, file))

                    # Remove empty lvl2 folder (if not empty due to hidden files, leave it)
                    try:
                        os.rmdir(item_path)
                    except OSError:
                        pass  # Directory not empty (likely has hidden files)

                elif os.path.isfile(item_path):
                    # Direct file inside lvl1 → move to parent
                    shutil.move(item_path, os.path.join(parent_folder, item))

            # Remove empty lvl1 folder (if not empty due to hidden files, leave it)
            try:
                os.rmdir(lvl1_path)
            except OSError:
                pass  # Directory not empty (likely has hidden files)

        elif os.path.isfile(lvl1_path):
            # Already in parent, skip
            print(f"Error: File already in parent: {lvl1_path}")


def unzip_activity_data():
    # Get all the zip files (exclude hidden files)
    zip_files = sorted(
        [f for f in Path(file_path).glob("*.zip") if not is_hidden_file(str(f))],
        key=lambda f: f.name
    )
    output_folders = ["activity", "rr_interval", "sleep", "temperature"]
    print("\n \n Unzipping activity data... \n")

    # Create the output folders if they do not exist
    for folder in output_folders:
        path = output_path + constants.folder_delimiter + folder
        os.makedirs(path, exist_ok=True)

    if zip_files:
        # --- Legacy format: sdk-export-<timestamp>.zip ---
        # Create temp folder for unzipping
        temp_path = output_path + constants.folder_delimiter + "temp"
        os.makedirs(temp_path, exist_ok=True)

        for zip_file in zip_files:
            print(f"Unzipping file: {zip_file.name} \n")
            zip_timestamp = zip_file.stem.split("sdk-export")[1]
            with zipfile.ZipFile(zip_file, "r") as zip_ref:
                zip_ref.extractall(temp_path)

            for extracted_file in os.listdir(temp_path):
                if is_hidden_file(extracted_file):
                    continue
                if extracted_file.endswith(".csv"):
                    if "activity" in extracted_file:
                        output_folder = "activity"
                    elif "hrv" in extracted_file:
                        output_folder = "rr_interval"
                    elif "sleep" in extracted_file:
                        output_folder = "sleep"
                    elif "export-temperature" in extracted_file:
                        output_folder = "temperature"
                    elif "export-extended-temperature" in extracted_file:
                        continue
                    else:
                        print(f"Unknown file type: {extracted_file}. Skipping.")
                        continue
                old_file = os.path.join(temp_path, extracted_file)
                new_filename = extracted_file.split(".csv")[0] + zip_timestamp + ".csv"
                new_file = os.path.join(output_path, output_folder, new_filename)
                os.rename(old_file, new_file)

        shutil.rmtree(os.path.join(output_path, "temp"))

    else:
        # --- New format: CSV files delivered directly (no zip) ---
        csv_files = sorted(
            [f for f in Path(file_path).glob("*.csv") if not is_hidden_file(str(f))],
            key=lambda f: f.name
        )
        for csv_file in csv_files:
            name_lower = csv_file.name.lower()
            if "activity" in name_lower:
                output_folder = "activity"
            elif "hrv" in name_lower:
                output_folder = "rr_interval"
            elif "sleep" in name_lower:
                output_folder = "sleep"
            elif "extended-temperature" in name_lower:
                continue
            elif "temperature" in name_lower:
                output_folder = "temperature"
            else:
                print(f"Unknown CSV file: {csv_file.name}. Skipping.")
                continue
            dest = os.path.join(output_path, output_folder, csv_file.name)
            shutil.copy2(str(csv_file), dest)
            print(f"Copied {csv_file.name} → {output_folder}/")


def copy_emography_data():
    # Get all the emography files (exclude hidden files)
    emography_files = sorted(
        [f for f in Path(file_path).glob("Emography-*.csv") if not is_hidden_file(str(f))],
        key=lambda f: f.name
    )

    # Create the output folder
    path = output_path + constants.folder_delimiter + "emography"
    os.makedirs(path, exist_ok=True)

    # Move them to the output folder
    print("\n \n Copying emography data... \n")
    print("Number of emography files found:", len(emography_files))
    for emography_file in emography_files:
        # Copy the file to the output folder
        old_file = os.path.join(file_path, emography_file.name)
        new_file = os.path.join(path, emography_file.name)
        print(f"Copying: {old_file} to {new_file}")
        shutil.copy(old_file, new_file)


def join_files_per_date(data_types):
    for data_type in data_types:
        # Folder containing your CSVs
        csv_folder = Path(output_path + constants.folder_delimiter + data_type)
        csv_files = sorted(
            [f for f in Path(csv_folder).glob("*.csv") if not is_hidden_file(str(f))],
            key=lambda f: f.name
        )
        print(f"\nJoining files for data type: {data_type}")

        # Iterate over the CSV files and store them when appropriate
        for csv_file in csv_files:
            date_column_name = "date"

            # Load csv to dataframe per chunks
            for chunk in pd.read_csv(csv_file, chunksize=100_000):

                # Drop the 'bioz' column if it exists
                if data_type == "bioz":
                    chunk = chunk.drop(columns=" BioZ ")
                elif data_type == "activity":
                    chunk = chunk.drop(
                        columns=[
                            " NIBP SYS",
                            " NIBP DIA",
                            " NIBP SYS long term",
                            " NIBP DIA long term",
                            " NIBP SYS combine",
                            " NIBP DIA combine",
                        ]
                    )
                    chunk = chunk.loc[:, ~chunk.columns.str.contains("^Unnamed")]
                elif data_type == "sleep":
                    chunk = chunk.drop(columns="STATE.1")
                elif data_type == "temperature":
                    chunk["TEMP1"] = chunk["TEMP1"].replace(0.0, np.nan)
                    chunk["TEMP2"] = chunk["TEMP2"].replace(0.0, np.nan)
                chunk.columns = chunk.columns.str.strip().str.lower()

                # Create a temporal column for filtering the date
                chunk["temp_date"] = (
                    chunk[date_column_name].astype(str).str.split("T").str[0]
                )

                # Group the chunk by date
                for date, group in chunk.groupby("temp_date"):
                    # Check if the group is empty
                    if group.empty:
                        continue
                    # Create the output directory for the date if it doesn't exist
                    output_dir = os.path.join(output_path, date)
                    os.makedirs(output_dir, exist_ok=True)

                    if data_type == "ppg":
                        # Group again by the metric id
                        for metric_id, metric_group in group.groupby("metric_id"):
                            if metric_id == "0x7b":
                                file_name = f"{data_type}2_infra_red_22"
                            elif metric_id == "0x7c":
                                file_name = f"{data_type}2_red_182"
                            elif metric_id == "0x7d":
                                file_name = f"{data_type}2_red_middle"
                            elif metric_id == "0x7e":
                                file_name = f"{data_type}2_green_6"
                            elif metric_id == "0x7f":
                                file_name = f"{data_type}2_ambinet"

                            write_joined_data(
                                output_dir, data_type, file_name, metric_group
                            )
                    else:
                        write_joined_data(output_dir, data_type, data_type, group)

        # Remove the whole folder after processing
        shutil.rmtree(csv_folder, ignore_errors=True)


def write_joined_data(output_dir, data_type, file_name, data):
    output_file = os.path.join(output_dir, f"{file_name}.csv")

    # Remove the temporary date column
    data = data.drop(columns="temp_date")
    # Remove any duplicated rows
    data = data.drop_duplicates()
    # Write header
    data.columns = constants.data_type_headers[data_type]

    if data_type == "sleep" and os.path.exists(output_file):
        existing_data = pd.read_csv(output_file)
        data = pd.concat([existing_data, data], ignore_index=True)
        print(
            f"Appending data to {output_file} with header read{existing_data.columns.tolist()} + current {data.columns.tolist()}"
        )
        # Remove duplicates after concatenation
        data = data.drop_duplicates()
        # Save the data to the output file
        data.to_csv(output_file, mode="w", index=False, na_rep="")
    else:
        # Save the data to the output file
        data.to_csv(
            output_file,
            mode="a",
            index=False,
            na_rep="",
            header=not os.path.exists(output_file),
        )

# def run_wiff_parsing(path_to_parse, is_japan_timezone=False, is_manual_data=True):
#     # Declare globals that are used across functions
#     global file_path, output_path, time_zone, input_file_name
#     global ppg_times, bioz_time, acc_time, ecg_time
#     global open_files, file_names, bioz_outputfile, acc_outputfile

#     print("\n \n Starting Corsano Wiff parsing... \n")
    
#     # Initialize global variables
#     ppg_times = np.zeros(8 * 2, dtype=np.int64)
#     bioz_time = 0
#     acc_time = 0
#     ecg_time = 0
#     open_files = []
#     file_names = []
#     bioz_outputfile = None
#     acc_outputfile = None
#     input_file_name = None
    
#     # Create output path
#     base_output_path = path_to_parse + "_formatted"
#     os.makedirs(base_output_path, exist_ok=True)
#     path_to_parse += constants.folder_delimiter
#     time_zone = tz.gettz("Asia/Tokyo") if is_japan_timezone else tz.gettz("Europe/Paris")
#     data_paths = (
#         constants.output_folder_paths.values() if is_manual_data else constants.folder_paths
#     )

#     # Check the content of each of the following folders
#     for folder_type in data_paths:
#         file_path = path_to_parse + folder_type + constants.folder_delimiter
#         if is_manual_data:
#             flatten_folders(file_path)
#             output_folder_name = folder_type
#         else:
#             output_folder_name = constants.output_folder_paths[folder_type]
#         print("Processing folder:", file_path)
#         if os.path.isdir(os.path.join(path_to_parse, folder_type)):
#             output_path = (
#                     base_output_path
#                     + constants.folder_delimiter
#                     + output_folder_name
#                     + constants.folder_delimiter
#             )
#             # Create output path for each folder
#             os.makedirs(output_path, exist_ok=True)
#             if output_folder_name == "corsano_bioz":
#                 copy_emography_data()
#                 os.makedirs(output_path + "bioz", exist_ok=True)
#             else:
#                 os.makedirs(output_path + "ppg", exist_ok=True)
#             os.makedirs(output_path + "acc", exist_ok=True)
#             parse_data()
#             unzip_activity_data()
#             join_files_per_date(constants.data_type_per_corsano[output_folder_name])

def run_wiff_parsing(path_to_parse, is_japan_timezone=False, is_manual_data=True):
    # Declare globals that are used across functions
    global file_path, output_path, time_zone, input_file_name
    global ppg_times, bioz_time, acc_time, ecg_time
    global open_files, file_names, bioz_outputfile, acc_outputfile

    try:
        print("\n\nStarting Corsano Wiff parsing...")

        # Initialize global variables (fresh state for each run)
        ppg_times = np.zeros(8 * 2, dtype=np.int64)
        bioz_time = 0
        acc_time = 0
        ecg_time = 0
        open_files = []
        file_names = []
        bioz_outputfile = None
        acc_outputfile = None
        input_file_name = None

        # Normalize input path (do not mutate caller's string)
        base_input = str(path_to_parse).rstrip("\\/")

        # Create output path next to input
        base_output_path = base_input + "_formatted"
        os.makedirs(base_output_path, exist_ok=True)

        # Time zone
        time_zone = tz.gettz("Asia/Tokyo") if is_japan_timezone else tz.gettz("Europe/Paris")

        # Decide which folder list to iterate
        data_paths = (
            constants.output_folder_paths.values() if is_manual_data else constants.folder_paths
        )

        # Loop through each folder type and process if exists
        for folder_type in data_paths:
            # compute folder paths (safe joins)
            folder_name = folder_type
            input_folder = os.path.join(base_input, folder_name)
            file_path = os.path.join(input_folder, "")  # ensure trailing separator for existing code

            # determine output folder name (same as before)
            if is_manual_data:
                output_folder_name = folder_name
            else:
                # when using constants.folder_paths, folder_type is the key; guard lookup
                output_folder_name = constants.output_folder_paths.get(folder_name, folder_name)

            print("Processing folder:", file_path)
            if os.path.isdir(input_folder):
                output_path = (
                    base_output_path
                    + constants.folder_delimiter
                    + output_folder_name
                    + constants.folder_delimiter
                )
                # Create output path for each folder
                os.makedirs(output_path, exist_ok=True)

                # Prepare subfolders used later by other functions
                try:
                    if output_folder_name == "corsano_bioz":
                        copy_emography_data()
                        os.makedirs(os.path.join(output_path, "bioz"), exist_ok=True)
                    else:
                        os.makedirs(os.path.join(output_path, "ppg"), exist_ok=True)
                except Exception:
                    # don't fail entire run for minor folder creation issues
                    pass

                os.makedirs(os.path.join(output_path, "acc"), exist_ok=True)

                # parse binary wiff files in this folder
                parse_data()

                # activity zips and other CSVs (if any)
                unzip_activity_data()

                # join per-date CSVs if mapping exists
                try:
                    mapping = constants.data_type_per_corsano.get(output_folder_name)
                    if mapping:
                        join_files_per_date(mapping)
                except Exception:
                    print("Warning: could not join files for", output_folder_name)

        return True
    except Exception as e:
        print("Exception in run_wiff_parsing:", type(e), e)
        traceback.print_exc()
        return False

if __name__ == "__main__":
    # Get the arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("filepath", help="Path of the data to parse")
    parser.add_argument(
        "--japanTimezone", action="store_true", help="Enable Japan timezone"
    )
    parser.add_argument(
        "--isManualData", action="store_true", help="Enable manual data mode"
    )
    args = parser.parse_args()
    run_wiff_parsing(args.filepath, args.japanTimezone, args.isManualData)


