import os
import sys

# Ensure the root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set BREGGER_WORKDIR to the current development directory so skills are loaded correctly
os.environ["BREGGER_WORKDIR"] = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
