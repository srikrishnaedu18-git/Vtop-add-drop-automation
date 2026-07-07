# Use official Playwright Python image which has python, browsers, and all system packages pre-installed
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set python environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and files
COPY . .

# Set chrome path to use the pre-installed chromium browser in the playwright docker image
ENV CHROME_PATH=/usr/bin/google-chrome

EXPOSE 8080

# Run uvicorn server on startup
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
