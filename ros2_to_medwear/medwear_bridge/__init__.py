"""MedWear ROS 2 bridge: healthcare_msgs topics -> MedWear batch API.

Modules: client (API), message_types (supported messages), info (Info
messages -> header metadata), payload (batches), buffering (batch window),
uploader (upload workers, metrics), tracking (staleness), handler (per topic).
"""
