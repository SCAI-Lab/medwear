'''
Adapted from the Sensor-Data-Parsing repository.
Author: Alan Savio Paul et al. (SCAI Lab, ETH Zürich)
License: Apache License 2.0
Source: https://github.com/SCAI-Lab/sensor-data-parsing
'''

import pandas as pd
import os
from tqdm import tqdm
# import logging
from parselib import logger, settings
from datetime import datetime
from pathlib import Path

class CorsanoParser:

    def __init__(self, date_folders, out_dir, input_dir, projectid, userid):
        self.date_folders = date_folders
        self.out_dir = out_dir
        logger.info(f"Output dir: {self.out_dir}")
        self.input_dir = input_dir
        self.projectid = projectid
        self.userid = userid

    def parse(self, date_folder):

        files_to_ignore = ["ecg.csv", "surveys.csv", "respiration.csv", "activity.csv", "workout.csv"]
        # semicolon_formatted_files = ["temperature.csv", "rr.csv", "emography.csv", "sleep.csv"]
        semicolon_formatted_files = []

        for f in os.listdir(date_folder):
            if f in files_to_ignore:
                continue
            with open(os.path.join(date_folder, f), 'rb') as text_file:
                f_length = len(text_file.readlines())
            if f_length <= 1:
                continue
            data = pd.read_csv(os.path.join(date_folder, f), sep=";" if f in semicolon_formatted_files else ",")
            logger.debug(f"{os.path.join(date_folder, f)}: {data.columns}")
            data.insert(1, 'projectId', self.projectid)
            data.insert(2, 'userId', self.userid)
            if 'time' in data.columns or 'timestamp' in data.columns:
                data.rename(columns={"timestamp": "time", "offset": "offset_column"}, inplace=True)
                data['time'] = data["time"]/1000
                if 'date' in data.columns:
                    data.drop(columns="date", axis=1, inplace=True)

            if 'date' in data.columns:
                time_col = data['date'].map(lambda x: datetime.timestamp(datetime.fromisoformat(x)), na_action='ignore')
                data.insert(0, 'time', time_col)
                data.drop(columns="date", axis=1, inplace=True)
            # Use datetime.fromisoformat()
            if 'wrist' in self.input_dir:
                out_file_path = os.path.join(self.out_dir, f'corsano_wrist_{f[:-4]}')
            elif 'bioz' in self.input_dir:
                out_file_path = os.path.join(self.out_dir, f'corsano_bioz_{f[:-4]}')
            elif 'arm' in self.input_dir:
                out_file_path = os.path.join(self.out_dir, f'corsano_arm_{f[:-4]}')
            elif 'left' in self.input_dir:
                out_file_path = os.path.join(self.out_dir, f'corsano_left_{f[:-4]}')
            elif 'right' in self.input_dir:
                out_file_path = os.path.join(self.out_dir, f'corsano_right_{f[:-4]}')
                
            if not os.path.exists(out_file_path):
                os.makedirs(out_file_path)
            # Note: this method assumes that only one file for each reading is created
            data.to_csv(Path(out_file_path, str(Path(date_folder).name) + '.csv.gz'), index=False, compression='gzip')
            # data.to_csv(Path(self.out_dir, f'corsano_wrist_{f[:-4]}', str(Path(date_folder).name) + '.csv'), index=False, compression=None)

    def export(self):
        print(f"Date_folders: {self.date_folders}")

        for date in tqdm(self.date_folders):
            if not os.path.isdir(os.path.join(self.input_dir, date)):
                continue
            csv_file_exists = any([x.endswith(".csv") for x in os.listdir(os.path.join(self.input_dir, date))])
            if not csv_file_exists:
                continue
            self.parse(os.path.join(self.input_dir, date))
        logger.info(f"Finished parsing CORSANO files to interim csv files.")