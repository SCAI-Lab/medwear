from setuptools import setup, find_packages

setup(
    name="medwear_med_api",
    version="0.1.0",
    author="Monica Perez Serrano",
    author_email="monica.perez@llui.org",
    description="API and router utilities for Med API project",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    packages=find_packages(exclude=["tests*", "test*"]),
    python_requires=">=3.10",
    install_requires=[
        "fastapi",
        "asyncpg",
        "python-jose[cryptography]",
        "influxdb-client",
        "sqlmodel",
        "uvicorn",
        "pytest",
        "httpx",
    ],
    classifiers=[
        "Programming Language :: Python :: 3.10",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    include_package_data=True,
    zip_safe=False,
    data_files=[
        ('/usr/share/medwear_med_api', ['docker/docker-compose-med-api.yaml']),
    ],
)
