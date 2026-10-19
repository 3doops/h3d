"""Checklist for the HotA 3D local mode (game folder, Python/frida/websockets, on Linux Wine): what is missing and
how to fix it. Same as godot/bridge/doctor.py (which also ships inside the client packages as bridge/doctor.py).

    python godot/tools/doctor.py [--game-dir DIR] [--json] [--find]
"""
import os
import runpy
import sys

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'bridge')
sys.path.insert(0, os.path.normpath(BRIDGE))
sys.argv[0] = os.path.join(BRIDGE, 'doctor.py')
runpy.run_path(os.path.join(BRIDGE, 'doctor.py'), run_name='__main__')
