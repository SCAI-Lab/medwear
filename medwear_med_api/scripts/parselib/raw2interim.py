from parselib.parsers.sensor_parsers.viva_lnk.viva_lnk_parser import export_vivalnk
from parselib.parsers.sensor_parsers.viva_lnk.viva_lnk_parser_v2 import export_vivalnk_v2
from parselib.parsers.sensor_parsers.corsano.corsano_parser import *
from parselib.parsers.sensor_parsers.corsano.corsano_formatter import *
from parselib.parsers.Core_parser import *
from parselib.parsers.Biobeat_parser import *
from parselib.parsers.Cosinuss_parser import *
from parselib.parsers.Sensomativephone_parser import *
from parselib.parsers.Sensomativelogger_parser import *
from parselib.parsers.ZurichMove_parser import *
from parselib.parsers.SCAIApp_parser import *
from parselib.parsers.iHealth_parser import *
from parselib.parsers.BP_parser import *
from parselib.parsers.Fitbit_parser import *
from parselib.parsers.Oura_parser import *
from parselib.parsers.Mbient_parser import *
from parselib.parsers.Log_parser import *
from parselib.parsers.Sleep_scanner_parser import *
from parselib.parsers.Somnomedics_parser import *
from parselib.parsers.Axivity_parser import *
import os
from parselib import logger, settings
from glob import glob
import yaml


if __name__ == '__main__':
    config = yaml.safe_load(open(settings.CONFIG_PATH, 'r'))
    project = config['project']
    out_dir = settings.INTERIM_DATA_PATH #config['interim_data_dir']
    input_dir = settings.RAW_DATA_PATH
    arg_user = config['user']
    sensor_type = config['sensor_type']
    logger.info(os.path.join(input_dir, project))
    for user in os.listdir(os.path.join(input_dir, project)):
        if user != arg_user and arg_user != "all":
            continue
        user_output_dir = os.path.join(out_dir, project, user)
        
        # if sensor_type == 'vivaLNK_ecg' or sensor_type == 'all':
        #     files_in_chunk = 3
        #     user_input_dir = os.path.join(input_dir, project, user, 'vivaLNK_ecg')
        #     if os.path.isdir(user_input_dir):
        #         files = os.listdir(user_input_dir)
        #         logger.info(f"Files: {files}")
        #         export_vivalnk(files, files_in_chunk, user_output_dir, user_input_dir, project, user)

        if sensor_type == 'vivaLNK_ecg' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'vivaLNK_ecg')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                logger.info(f"Files: {files}")
                export_vivalnk_v2(files, user_output_dir, user_input_dir)

        if sensor_type == 'sensomative_phone' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'sensomative_phone')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                print(user_output_dir)
                sensomative_parser = SensomativeParser(files, user_output_dir, user_input_dir, project, user)
                sensomative_parser.export()

        if sensor_type == 'sensomative_back_logger' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'sensomative_back_logger')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                print(user_output_dir)
                sensomative_parser = SensomativeLoggerParser(files, user_output_dir, user_input_dir, project, user)
                sensomative_parser.export()
                
        if sensor_type == 'sensomative_bottom_logger' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'sensomative_bottom_logger')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                print(user_output_dir)
                sensomative_parser = SensomativeLoggerParser(files, user_output_dir, user_input_dir, project, user)
                sensomative_parser.export()
                
        if sensor_type == 'sensomative_logger' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'sensomative_logger')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                print(user_output_dir)
                sensomative_parser = SensomativeLoggerParser(files, user_output_dir, user_input_dir, project, user)
                sensomative_parser.export()

        if sensor_type == 'zurichmove_imu' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'zurichmove_imu')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                zurichmove_parser = ZurichMoveParser(files, user_output_dir, user_input_dir, project, user)
                zurichmove_parser.export()
                
        # if sensor_type == 'vivaLNK_temp' or sensor_type == 'all':
        #     user_input_dir = os.path.join(input_dir, project, user, 'vivaLNK_temp')
        #     if os.path.isdir(user_input_dir):
        #         files = os.listdir(user_input_dir)
        #         temperature_parser = VivaLnkTemperatureParser(files, user_output_dir, user_input_dir, project, user)
        #         tempeature_parser.export()

        if sensor_type.startswith("corsano") or sensor_type == "all":
            # then parse new corsano data structure
            debug = True
            if not os.path.exists(os.path.join(input_dir, project, user + '_formatted')) or debug:
                user_input_dir = os.path.join(input_dir, project, user)
                # Get timezone setting from config if available, otherwise default to False
                japan_timezone = config.get('japan_timezone', False)
                corsano_formatter = CorsanoFormatter(user_input_dir, project, user, japan_timezone)
                corsano_formatter.export()
            else:
                logger.info(f"Formatted Corsano data already exists for user {user}. Skipping formatting step.")
            # continue with normal parsing
        
        if sensor_type == "corsano_wrist" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user + '_formatted', 'corsano_wrist')  # check whether it exists to cut down on processing time?
            logger.info(f"Parsing Corsano_wrist. Raw directory: {user_input_dir}")
            if os.path.isdir(user_input_dir):
                date_folders = os.listdir(user_input_dir)
                corsano_parser = CorsanoParser(date_folders, user_output_dir, user_input_dir, project, user)
                corsano_parser.export()

        if sensor_type == "corsano_bioz" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user + '_formatted', 'corsano_bioz')
            logger.info(f"Parsing Corsano_bioz. Raw directory: {user_input_dir}")
            if os.path.isdir(user_input_dir):
                date_folders = os.listdir(user_input_dir)
                corsano_parser = CorsanoParser(date_folders, user_output_dir, user_input_dir, project, user)
                corsano_parser.export()
                           
        if sensor_type == "corsano_arm" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'corsano_arm')
            logger.info(f"Parsing Corsano. Raw directory: {user_input_dir}")
            if os.path.isdir(user_input_dir):
                date_folders = os.listdir(user_input_dir)
                corsano_formatter = CorsanoFormatter(date_folders, user_input_dir, project, user)
                corsano_formatter.export()
                corsano_parser = CorsanoParser(date_folders, user_output_dir, user_input_dir, project, user)
                corsano_parser.export()

        if sensor_type == "corsano_left" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'corsano_left')
            logger.info(f"Parsing Corsano. Raw directory: {user_input_dir}")
            if os.path.isdir(user_input_dir):
                date_folders = os.listdir(user_input_dir)
                corsano_parser = CorsanoParser(date_folders, user_output_dir, user_input_dir, project, user)
                corsano_parser.export()

        if sensor_type == "corsano_right" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'corsano_right')
            logger.info(f"Parsing Corsano. Raw directory: {user_input_dir}")
            if os.path.isdir(user_input_dir):
                date_folders = os.listdir(user_input_dir)
                corsano_parser = CorsanoParser(date_folders, user_output_dir, user_input_dir, project, user)
                corsano_parser.export()

        # if sensor_type == "fitbit" or sensor_type == "all":
        #     # Loads the fitbit data from the api
        #     fitbit_parser = FitbitParser(config['start_date'], config['end_date'], user_output_dir, project, user)
        #     fitbit_parser.export()

        if sensor_type == "oura_ring" or sensor_type == "all":
            # Loads the oura data from the api
            oura_parser = OuraParser(config['start_date'], config['end_date'], user_output_dir, project, user)
            oura_parser.export()

        if sensor_type == "biobeat_wrist" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'biobeat_wrist')

            if os.path.isdir(user_input_dir):

                csv_files = glob(f'{user_input_dir}/*.csv')
                if len(csv_files)  == 0:
                    logger.warning("No biobeat csv file found of the following (regex) format: BioBeat*.csv.")
                else:
                    csv_file = glob(f'{user_input_dir}/*.csv')[0]
                    csv_file_path = os.path.join(user_input_dir, csv_file)
                    if os.path.exists(csv_file_path):
                        biobeat_parser = BiobeatParser(csv_file_path, user_output_dir, user_input_dir, project, user)
                        biobeat_parser.export()
                    else:
                        logger.debug(f'Could not find file: {os.path.exists(os.path.join(user_input_dir, csv_file))}')

        if sensor_type == "cosinuss_ear" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'cosinuss_ear')

            if os.path.isdir(user_input_dir):
                logger.info(f"Number of files to parse in cosinuss_ear: {len(os.listdir(user_input_dir))}")
                cosinuss_parser = CosinussParser(user_output_dir, user_input_dir, project, user)
                cosinuss_parser.export()

        if sensor_type == "mbient_imu_wc" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'mbient_imu_wc')
            if os.path.isdir(user_input_dir):
                print(f'Directory: {user_input_dir}')
                print(f'files in directory: {os.listdir(user_input_dir)}')
                logger.info(f"Number of Accelerometer files to parse in mbient: {len(glob(user_input_dir + '/*Accelerometer*.csv'))}")
                logger.info(f"Number of Gyroscope files to parse in mbient: {len(glob(user_input_dir + '/*Gyroscope*.csv'))}")
                mbient_parser = MbientParser(user_output_dir, user_input_dir, project, user)
                mbient_parser.export()
                
        if sensor_type == "mbient_imu_wrist" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'mbient_imu_wrist')

            if os.path.isdir(user_input_dir):
                logger.info(f"Number of Accelerometer files to parse in mbient: {len(glob(user_input_dir + '/*Accelerometer*.csv'))}")
                logger.info(f"Number of Gyroscope files to parse in mbient: {len(glob(user_input_dir + '/*Gyroscope*.csv'))}")
                mbient_parser = MbientParser(user_output_dir, user_input_dir, project, user)
                mbient_parser.export()

        if sensor_type == "core_temp" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'core_temp')

            # Check if the user_input_dir is a directory and contains CSV files
            if os.path.isdir(user_input_dir):
                csv_files = [file for file in os.listdir(user_input_dir) if file.endswith('.csv')]
                if csv_files:
                    for csv_file in csv_files:
                        core_parser = CoreParser(csv_file, user_output_dir, user_input_dir, project, user)
                        core_parser.export()
                else:
                    print("No CSV files found in the directory:", user_input_dir)

        if sensor_type == "scai_app" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'scai_app')

            if os.path.isdir(user_input_dir):
                scai_app_parser = SCAIAppParser(user_output_dir, user_input_dir)
                scai_app_parser.export()
                
        if sensor_type == "iHealth_bp" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'iHealth_bp')
            if os.path.isdir(user_input_dir) and '.csv' in os.listdir(user_input_dir)[0]:
                ihealth_parser = iHealthParser(os.listdir(user_input_dir)[0], user_output_dir, user_input_dir)
                ihealth_parser.export()

        if sensor_type == "bp_urodynamics" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'bp_urodynamics')
            if os.path.isdir(user_input_dir):
                bp_parser = BPParser(user_output_dir, user_input_dir)
                bp_parser.export()

        if sensor_type == "bp_oscillometry" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'bp_oscillometry')
            if os.path.isdir(user_input_dir):
                bp_parser = BPParser(user_output_dir, user_input_dir)
                bp_parser.export()

            if sensor_type == "bp_tilttable" or sensor_type == "all":
                user_input_dir = os.path.join(input_dir, project, user, 'bp_tilttable')
                if os.path.isdir(user_input_dir):
                    bp_parser = BPParser(user_output_dir, user_input_dir)
                    bp_parser.export()
                
        if sensor_type == "manual_log" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'manual_log')
            if os.path.isdir(user_input_dir):
                log_parser = LogParser(user_output_dir, user_input_dir)
                log_parser.export()

        if sensor_type == 'sleep_scanner' or sensor_type == 'all':
            if os.path.exists(os.path.join(input_dir, project, user, 'sleep_scanner/processed')):#for one subject it is lower case the other it is upper case
                user_input_dir = os.path.join(input_dir, project, user, 'sleep_scanner/processed')
            elif os.path.exists(os.path.join(input_dir, project, user, 'sleep_scanner/PROCESSED')):
                user_input_dir = os.path.join(input_dir, project, user, 'sleep_scanner/PROCESSED')
            else:
                user_input_dir = os.path.join(input_dir, project, user, 'sleep_scanner')
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                print(user_output_dir)
                sleep_scanner_parser = SleepScannerParser(files, user_output_dir, user_input_dir, project, user)
                sleep_scanner_parser.export()
                
        if sensor_type == 'somnomedics' or sensor_type == 'all':
            user_input_dir = os.path.join(input_dir, project, user, 'somnomedics') 
            if os.path.isdir(user_input_dir):
                files = os.listdir(user_input_dir)
                print(user_output_dir)
                somnomedics_parser = SomnomedicsParser(files, user_output_dir, user_input_dir, project, user)
                somnomedics_parser.export()

        if sensor_type == "axivity_imu_wc" or sensor_type == "all":
            user_input_dir = os.path.join(input_dir, project, user, 'axivity_imu_wc')
            if os.path.isdir(user_input_dir):
                print(f'Directory: {user_input_dir}')
                print(f'files in directory: {os.listdir(user_input_dir)}')
                axivity_parser = AxivityParser(user_output_dir, user_input_dir, project, user)
                axivity_parser.export()