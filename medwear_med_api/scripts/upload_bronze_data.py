#!/usr/bin/env python3
"""Upload test iActivity data to MinIO bronze bucket - supports multiple formats"""

import json
import sys
import os
from pathlib import Path
from minio import Minio
from datetime import datetime

# Import libraries for different formats
try:
    import pandas as pd
    import pyarrow.parquet as pq
    import fastavro
    EXTENDED_FORMATS = True
except ImportError:
    EXTENDED_FORMATS = False
    print("Warning: Extended format support not available. Install: pip install pandas pyarrow fastavro")

def detect_file_format(file_path):
    """Detect file format based on file extension"""
    ext = Path(file_path).suffix.lower()
    format_map = {
        '.json': 'json',
        '.parquet': 'parquet', 
        '.csv': 'csv',
        '.avro': 'avro',
        '.txt': 'txt',
        '.cwa': 'cwa'
    }
    return format_map.get(ext, 'json')  # Default to json

def read_file_data(file_path, file_format):
    """Read various file formats and convert to JSON-compatible data"""
    try:
        if file_format == 'json':
            with open(file_path, 'r') as f:
                return json.load(f)
        
        elif file_format == 'parquet' and EXTENDED_FORMATS:
            df = pd.read_parquet(file_path)
            return df.to_dict('records')
        
        elif file_format == 'csv' and EXTENDED_FORMATS:
            df = pd.read_csv(file_path)
            return df.to_dict('records')
        
        elif file_format == 'avro' and EXTENDED_FORMATS:
            records = []
            with open(file_path, 'rb') as f:
                reader = fastavro.reader(f)
                for record in reader:
                    records.append(record)
            return records
        
        elif file_format == 'txt':
            with open(file_path, 'r') as f:
                lines = f.readlines()
                records = []
                for i, line in enumerate(lines):
                    if '=' in line:
                        parts = line.strip().split('=', 1)
                        records.append({parts[0]: parts[1]})
                    else:
                        records.append({'line': i, 'content': line.strip()})
                return records
        
        elif file_format == 'cwa':
            # Basic CWA file handling
            file_size = os.path.getsize(file_path)
            return [{
                'file_type': 'cwa',
                'file_size': file_size,
                'file_path': str(file_path),
                'raw_data_available': True,
                'processing_required': True,
                'note': 'CWA file requires specialized processing pipeline'
            }]
        
        else:
            # Fallback: try to read as JSON
            with open(file_path, 'r') as f:
                return json.load(f)
            
    except Exception as e:
        raise Exception(f"Error reading {file_format} file: {str(e)}")

def upload_to_bronze(
    file_path: str,
    patient_id: str = "test_patient_001",
    minio_endpoint: str = "localhost:9000",
    access_key: str = "minioadmin",
    secret_key: str = "minioadmin123"
):
    """Upload file to MinIO bronze bucket with format detection"""
    client = Minio(
        minio_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=False
    )
    
    bucket_name = "medwear-bronze"
    
    try:
        if not client.bucket_exists(bucket_name):
            print(f"Creating bucket '{bucket_name}'")
            client.make_bucket(bucket_name)
            print(f"Bucket '{bucket_name}' created successfully")
    except Exception as e:
        print(f"Bucket operation error: {str(e)}")
        return False
    
    # Detect file format
    file_format = detect_file_format(file_path)
    original_filename = Path(file_path).name
    
    print(f"Detected file format: {file_format}")
    
    # Read file data based on format
    try:
        data = read_file_data(file_path, file_format)
    except Exception as e:
        print(f"ERROR: {str(e)}")
        return False
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    object_name = f"{patient_id}/iactivity_{timestamp}_{original_filename}.json"
    
    # Convert to JSON and add metadata
    data_with_metadata = {
        'original_format': file_format,
        'original_filename': original_filename,
        'patient_id': patient_id,
        'upload_timestamp': timestamp,
        'data': data
    }
    
    json_str = json.dumps(data_with_metadata, indent=2, ensure_ascii=False)
    json_bytes = json_str.encode('utf-8')
    
    from io import BytesIO
    
    # Add metadata to MinIO object
    metadata = {
        "original-format": file_format,
        "patient-id": patient_id,
        "upload-timestamp": timestamp,
        "original-filename": original_filename
    }
    
    client.put_object(
        bucket_name,
        object_name,
        data=BytesIO(json_bytes),
        length=len(json_bytes),
        content_type='application/json',
        metadata=metadata
    )
    
    record_count = len(data) if isinstance(data, list) else 1
    print(f"Uploaded {record_count} records from {file_format} file to bronze/{object_name}")
    return True


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Upload test data to MinIO bronze bucket (supports JSON/CSV/Parquet/Avro/TXT/CWA)")
    parser.add_argument("file_path", help="Path to data file")
    parser.add_argument("--patient-id", default="test_patient_001", help="Patient ID")
    parser.add_argument("--endpoint", default="localhost:9000", help="MinIO endpoint")
    parser.add_argument("--access-key", default="minioadmin", help="MinIO access key")
    parser.add_argument("--secret-key", default="minioadmin123", help="MinIO secret key")
    
    args = parser.parse_args()
    
    if not Path(args.file_path).exists():
        print(f"ERROR: File not found: {args.file_path}")
        sys.exit(1)
    
    success = upload_to_bronze(
        args.file_path,
        args.patient_id,
        args.endpoint,
        args.access_key,
        args.secret_key
    )
    
    sys.exit(0 if success else 1)