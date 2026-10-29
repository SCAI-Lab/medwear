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
        filepath = os.path.join(data_dir, file_name)
        print(f"Reading {file_name} from {data_dir}.")

        acc = {'t': [], 'x': [], 'y': [], 'z': []}
        ecg = {'t': [], 'val': []}
        hr = {'val': [], 't': []}
        rr = {'val': [], 't': []}

        with open(filepath, mode='r') as fid:
            line_number = 0
            while True:
                try:
                    line = fid.readline()
                    if not line:
                        break
                    if line == '' or not line.startswith('20'):
                        continue

                    json_data = '{' + line.split("{")[1]
                    data_dict = json.loads(json_data)

                except Exception as e:
                    print(f"[ERROR] Line {line_number}: {e}")
                    continue

                date_time = data_dict['recordTime']

                for kk in range(self.fs_ecg):
                    ecg['t'].append(date_time + kk * self.ts_ecg)
                    ecg['val'].append(data_dict['ecg'][kk])

                for kk in range(self.fs_acc):
                    acc['t'].append(date_time + kk * self.ts_acc)
                    acc['x'].append(data_dict['acc'][kk][0])
                    acc['y'].append(data_dict['acc'][kk][1])
                    acc['z'].append(data_dict['acc'][kk][2])

                hr['t'].append(date_time)
                hr['val'].append(data_dict['hr'])

                rr['t'].append(date_time)
                rr['val'].append(data_dict['rr'])

        # Convert to DataFrames and process directly
        ecg_df = pd.DataFrame({'time': ecg['t'], 'ecg': np.array(ecg['val']) / self.ecg_gain})
        acc_df = pd.DataFrame({
            'time': acc['t'],
            'x': acc['x'],
            'y': acc['y'],
            'z': acc['z']
        })
        hr_df = pd.DataFrame({'time': hr['t'], 'hr': hr['val']})
        rr_df = pd.DataFrame({'time': rr['t'], 'rr': rr['val']})

        # Sort by time and convert to seconds
        for df in [ecg_df, acc_df, hr_df, rr_df]:
            df.sort_values('time', inplace=True)
            df['time'] = df['time'] / 1000  # convert ms → seconds
            df.reset_index(drop=True, inplace=True)

        print(f"[SUCCESS] Parsing completed for {file_name}.")

        return ecg_df, acc_df, hr_df, rr_df



def export_vivalnk_v2(files, out_dir, input_dir):
    patch = vivaLNK()

    # Create output directories
    subdirs = {
        'hr': os.path.join(out_dir, 'vivalnk_vv330_heart_rate'),
        'rr': os.path.join(out_dir, 'vivalnk_vv330_resp_rate'),
        'ecg': os.path.join(out_dir, 'vivalnk_vv330_ecg'),
        'acc': os.path.join(out_dir, 'vivalnk_vv330_acceleration'),
    }
    for d in subdirs.values():
        os.makedirs(d, exist_ok=True)

    for file in tqdm(files):
        if file.endswith(".txt") and not fnmatch.fnmatch(file, 'FW_battery.txt'):
            try:
                ecg_df, acc_df, hr_df, rr_df = patch.parse(input_dir, file)

                base_name = os.path.splitext(file)[0] + ".csv.gz"

                hr_df.to_csv(os.path.join(subdirs['hr'], base_name), index=False, compression='gzip')
                rr_df.to_csv(os.path.join(subdirs['rr'], base_name), index=False, compression='gzip')
                ecg_df.to_csv(os.path.join(subdirs['ecg'], base_name), index=False, compression='gzip')
                acc_df.to_csv(os.path.join(subdirs['acc'], base_name), index=False, compression='gzip')

                print(f"[EXPORT DONE] {file} exported.\n")

            except Exception as e:
                print(f"[ERROR] Failed to process {file}: {e}\n")

    return True
