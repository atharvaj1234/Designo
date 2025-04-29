# Use an official Python runtime as a parent image
FROM python:3.13-slim

# Set environment variables
# Set the working directory in the container
WORKDIR /app

# Prevent Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE 1
# Ensure Python output is sent straight to output streams
ENV PYTHONUNBUFFERED 1

# Install system dependencies that might be needed (e.g., for Pillow or other libraries)
# Add any other build dependencies here if needed
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Install pip requirements
# Copy only requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Expose the port the app runs on (Cloud Run expects 8080 by default)
EXPOSE 8080

# Set the command to run the application using Gunicorn
# Number of workers can be adjusted based on expected load and instance size
# Use PORT environment variable provided by Cloud Run
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--worker-class", "sync", "app:app"]
# Note: For async (like Flask with await), you might need an async worker like uvicorn or gunicorn with gevent/asyncio workers.
# However, Flask's default dev server runs `await` fine, and sync workers often handle I/O bound tasks well enough.
# If using async workers (e.g., uvicorn): CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8080", "app:app"]
# Choose the worker type based on performance testing. Sync is simpler to start with.