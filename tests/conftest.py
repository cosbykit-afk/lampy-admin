"""Make the project root importable so tests can `import dbbrowser`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
