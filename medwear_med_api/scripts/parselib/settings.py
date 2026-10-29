from pathlib import Path
from dotenv import load_dotenv
import os

root = Path(__file__).parent.parent
load_dotenv(root / '.env')

CONFIG_PATH = root / 'config.yaml'
DATA_PATH = Path(os.environ['DATA_PATH'])
RAW_DATA_PATH = DATA_PATH / 'raw/'
INTERIM_DATA_PATH = DATA_PATH / 'interim/'
TEMP_FILE_PATH = DATA_PATH / 'temp/time_modified.csv.gz'
