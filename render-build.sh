#!/usr/bin/env bash
# exit on error
set -o errexit

# Install Python dependencies
pip install -r requirements.txt

# Install Node dependencies and build Tailwind CSS
npm install
npm run build:css

# (Optional) Run database migrations or initializations if needed
# python init_db_script.py 
