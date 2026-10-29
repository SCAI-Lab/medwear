'''
Adapted from the Sensor-Data-Parsing repository.
Author: Alan Savio Paul et al. (SCAI Lab, ETH Zürich)
License: Apache License 2.0
Source: https://github.com/SCAI-Lab/sensor-data-parsing
'''

from pathlib import Path
import pandas as pd
import os
from tqdm import tqdm
from parselib import logger, settings
from datetime import datetime
from parselib.parsers.sensor_parsers.corsano.corsano_parse_wiff import *

class CorsanoFormatter:

    def __init__(self, input_dir, projectid, userid, japan_timezone=False):  # include an out_dir?
        # self.out_dir = user_output_dir  # make a different output dir to keep formatted data separate from raw data
        # logger.info(f"Output dir: {self.out_dir}")
        self.input_dir = input_dir
        self.projectid = projectid
        self.userid = userid
        self.japan_timezone = japan_timezone 

    def process_files(self):
        if os.path.isdir(self.input_dir):
            try:
                run_wiff_parsing(self.input_dir, self.japan_timezone, is_manual_data=True)
                logger.info(f"Successfully formatted files in: {self.input_dir}")
            except Exception as e:
                logger.error(f"Error parsing {self.input_dir}: {e}")
                raise

    def export(self):
        self.process_files()
        # logger.info(f"Finished formatting wiff files to interim csv files in {self.out_dir}.")
        # return self.out_dir