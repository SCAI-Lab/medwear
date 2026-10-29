import os
import numpy as np
import json
from tqdm import tqdm
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import os
import shutil
import fnmatch
import sys

'''
% function VivaLnk_parser(data_dir, file_name)
%
% Python code that parses VivaLink data in .txt format.
%
% Inputs : 
%          - data_dir : path to file that needs to be converted
%          - file_name: file name of the file to convert, without extension 
% Outputs:
%          - ecg, acc, hr, rr 
%
% Example :
%         VivaLink_parser('/Users/gdapoia/data', 'demo')
%
% Notes : 
% ECG Specifics:
%    Sampling rate: 128, Resolution: 16 bits, gain 1000
%    Sampling rate: 256
% Accelerometer:
%    Sampling rate: 5 Hz Resolution: 14 bit resolution over +/- 4G range.
%    Sampling rate: 10 Hz
%
% Author: Giulia Da Poian, Mehdi Ejtehadi
% email: giulia.dap@gmail.com, mehdi.ejtehadi.edu@gmail.com
'''


class vivaLNK:

    def __init__(self):

        self.ecg_gain = 1000
        self.fs_ecg = 256
        self.ts_ecg = 1000 / self.fs_ecg
        self.fs_acc = 10
        self.ts_acc = 1000 / self.fs_acc
        self.start_time = np.nan
        self.end_time = np.nan
        self.ecg_t_sort = np.nan
        self.time_stamp = []

    def parse(self, data_dir, file_name):
        fid = open(os.path.join(data_dir, file_name), mode='r')
        print(f"Reading {file_name} now.")
        
        self.ecg_t_sort = np.nan

        acc = {'t': [], 'x': [], 'y': [], 'z': []}
        ecg = {'t': [], 'val': []}
        hr = {'val': [], 't': []}
        rr = {'val': [], 't': []}

        #Lines = fid.readlines()

        with fid:
            line_number = 0
            while True:
                try:
                    line = fid.readline()  # Read the next line
                    if not line:  # End of file
                        break
                    if line == '':
                        continue
                    if not line.startswith('20'):
                        continue

                    json_data = '{' + line.split("{")[1]
                    try:
                        data_dict = json.loads(json_data)
                    except Exception as e:
                        print(f"An error occurred coverting the line {line_number} to json: {e}")
                except UnicodeDecodeError as e: # reached first corrupted line of .txt-file:
                    print(f"Error reading line {line_number}: {e}")
                    print(f"Skipping to the next line.")
                    continue

                date_time = data_dict['recordTime']

                # read ECG line
                for kk in range(self.fs_ecg):
                    ecg['t'].append(date_time + kk * self.ts_ecg)
                    ecg['val'].append(data_dict['ecg'][kk])

                # read ACC
                for kk in range(self.fs_acc):
                    acc['t'].append(date_time + kk * self.ts_acc)
                    acc['x'].append(data_dict['acc'][kk][0])
                    acc['y'].append(data_dict['acc'][kk][1])
                    acc['z'].append(data_dict['acc'][kk][2])

                hr['val'].append(data_dict['hr'])
                hr['t'].append(data_dict['recordTime'])
                rr['val'].append(data_dict['rr'])
                rr['t'].append(data_dict['recordTime'])
            
        fid.close()

        # sort data
        ecg_t_sort, ecg_sort = zip(*sorted(zip(ecg['t'], ecg['val'])))
        hr_t_sort, hr_sort = zip(*sorted(zip(hr['t'], hr['val'])))
        rr_t_sort, rr_sort = zip(*sorted(zip(rr['t'], rr['val'])))
        acc_t_sort, acc_x_sort = zip(*sorted(zip(acc['t'], acc['x'])))
        acc_t_sort, acc_y_sort = zip(*sorted(zip(acc['t'], acc['y'])))
        acc_t_sort, acc_z_sort = zip(*sorted(zip(acc['t'], acc['z'])))

        ecg_mv = tuple(np.divide(ecg_sort, self.ecg_gain))  # convert to mV
        ecg['t'] = ecg_t_sort
        ecg['val'] = ecg_mv
        acc['t'] = acc_t_sort
        acc['x'] = acc_x_sort
        acc['y'] = acc_y_sort
        acc['z'] = acc_z_sort
        hr['val'] = hr_sort
        hr['t'] = hr_t_sort
        rr['val'] = rr_sort
        rr['t'] = rr_t_sort

        print(f"[SUCCESS] Parsing is completed for {file_name} .")

        return ecg, acc, hr, rr


def export_vivalnk(files, files_in_chunk, out_dir, input_dir, project, subject):
    patch = vivaLNK()

    for i in range(0, len(files), files_in_chunk):

        hr_t = tuple()
        hr_data = tuple()
        ecg_t = tuple()
        ecg_data = tuple()
        rr_t = tuple()
        rr_data = tuple()
        acc_t = tuple()
        acc_data_x = tuple()
        acc_data_y = tuple()
        acc_data_z = tuple()

        files_subset = files[i:i + 3]

        for file in tqdm(files_subset):
            if file.endswith(".txt") and not (fnmatch.fnmatch(file, 'FW_battery.txt')):
                try:
                    ecg_f, acc_f, hr_f, rr_f = patch.parse(input_dir, file)

                    ecg_t = ecg_t + ecg_f['t']
                    ecg_data = ecg_data + ecg_f['val']

                    hr_t = hr_t + hr_f['t']
                    hr_data = hr_data + hr_f['val']

                    rr_t = rr_t + rr_f['t']
                    rr_data = rr_data + rr_f['val']

                    acc_t = acc_t + acc_f['t']
                    acc_data_x = acc_data_x + acc_f['x']
                    acc_data_y = acc_data_y + acc_f['y']
                    acc_data_z = acc_data_z + acc_f['z']

                    print(f"[SUCCESS] {file} was parsed.")

                except Exception as e:
                    print("An error occurred while parsing: " + file)
                    print(e)

        hr = pd.DataFrame({'projectId': [], 'userId': [], 'time': [], 'hr': []})
        rr = pd.DataFrame({'projectId': [], 'userId': [], 'time': [], 'rr': []})
        ecg = pd.DataFrame({'projectId': [], 'userId': [], 'time': [], 'ecg': []})
        acc = pd.DataFrame({'projectId': [], 'userId': [], 'time': [], 'x': [], 'y': [], 'z': []})

        # sorting
        hr['time'], hr['hr'] = zip(*sorted(zip(hr_t, hr_data)))
        hr['projectId'] = project
        hr['userId'] = subject
        rr['time'], rr['rr'] = zip(*sorted(zip(rr_t, rr_data)))
        rr['projectId'] = project
        rr['userId'] = subject
        ecg['time'], ecg['ecg'] = zip(*sorted(zip(ecg_t, ecg_data)))
        ecg['projectId'] = project
        ecg['userId'] = subject
        acc['time'], acc['x'], acc['y'], acc['z'] = zip(*sorted(zip(acc_t, acc_data_x, acc_data_y, acc_data_z)))
        acc['projectId'] = project
        acc['userId'] = subject

        hr['time'] = hr['time'] / 1000
        rr['time'] = rr['time'] / 1000
        ecg['time'] = ecg['time'] / 1000
        acc['time'] = acc['time'] / 1000

        directory_hr = os.path.join(out_dir, 'vivalnk_vv330_heart_rate')
        directory_rr = os.path.join(out_dir, 'vivalnk_vv330_resp_rate')
        directory_ecg = os.path.join(out_dir, 'vivalnk_vv330_ecg')
        directory_acc = os.path.join(out_dir, 'vivalnk_vv330_acceleration')

        start_timestamp = hr['time'].iloc[0] - (
                    hr['time'].iloc[0] % 3600)  # get first time stamp and round up to the hour
        start_time = datetime.utcfromtimestamp(start_timestamp)  # .replace( minute=00, second =00, microsecond=0)

        one_hour = 3600
        days = np.abs(
            (datetime.utcfromtimestamp(hr['time'].iloc[0]) - datetime.utcfromtimestamp(hr['time'].iloc[-1])).days)

        for h in range(24 * days):

            min_time = start_timestamp + (h * one_hour)
            max_time = start_timestamp + ((h + 1) * one_hour)

            hr_hour = hr[(hr['time'] >= int(min_time)) & (hr['time'] < int(max_time))]
            rr_hour = rr[(rr['time'] >= min_time) & (rr['time'] < max_time)]
            ecg_hour = ecg[(ecg['time'] >= min_time) & (ecg['time'] < max_time)]
            acc_hour = acc[(acc['time'] >= int(min_time)) & (acc['time'] < int(max_time))]

            new = datetime.fromtimestamp(min_time, timezone.utc)


            for sensor, sensor_dir in [(hr_hour, directory_hr), (rr_hour, directory_rr), (ecg_hour, directory_ecg), (acc_hour, directory_acc)]:

                if not os.path.exists(sensor_dir):
                    os.makedirs(sensor_dir)

                if not sensor.empty:
                    day = datetime.utcfromtimestamp(datetime.timestamp(new)).strftime('%Y%m%d')
                    day_dir = os.path.join(sensor_dir, day)
                    if not os.path.exists(day_dir):
                        os.makedirs(day_dir)

                    fname = datetime.utcfromtimestamp(datetime.timestamp(new)).strftime('%Y%m%d_%H%M') + '.csv.gz'

                    if os.path.exists(os.path.join(day_dir, fname)):
                        df_tmp = pd.read_csv(os.path.join(day_dir, fname), compression='gzip')
                        frames = [df_tmp, sensor]
                        sensor = pd.concat(frames)
                        sensor = sensor.sort_values(by=['time'])
                        sensor = sensor.groupby('time').first().reset_index()

                    fwrite = os.path.join(day_dir, fname)
                    sensor.to_csv(fwrite, index=False, compression="gzip")

    return True
