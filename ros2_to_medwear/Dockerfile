# MedWear ROS 2 Bridge
# Base: ROS 2 Jazzy (Ubuntu 24.04) — matches Monica's lab setup
FROM ros:jazzy-ros-base

WORKDIR /bridge

# Install Python deps
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-colcon-common-extensions \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages requests pyyaml

# Build healthcare_msgs from SCAI-Lab
RUN mkdir -p /ros2_ws/src && \
    git clone https://github.com/SCAI-Lab/healthcare_msgs /ros2_ws/src/healthcare_msgs && \
    . /opt/ros/jazzy/setup.sh && \
    cd /ros2_ws && colcon build --symlink-install && \
    echo "source /ros2_ws/install/setup.bash" >> /etc/bash.bashrc

COPY config.yaml .
COPY medwear_subscriber.py .
COPY medwear_bridge/ medwear_bridge/

# Source ROS 2 + workspace on every exec
SHELL ["/bin/bash", "-c"]
ENTRYPOINT ["/bin/bash", "-c", \
    "source /opt/ros/jazzy/setup.bash && \
     source /ros2_ws/install/setup.bash && \
     python3 medwear_subscriber.py \"$@\"", "--"]
