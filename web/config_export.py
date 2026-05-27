"""Export database state to YAML config for qr_live.py."""

import yaml
from . import db


def export_config(output_path="config/doors.yaml"):
    """Export doors and active user tokens to YAML config."""
    doors = db.get_doors()
    # get_all_active_tokens returns a set (membership semantics for the
    # detector hot path); sort for stable YAML output.
    tokens = sorted(db.get_all_active_tokens())

    config = {'doors': []}
    for door in doors:
        entry = {
            'name': door['name'],
            'camera': f"onvif:uuid:{door['camera_uuid']}"
                      if door['camera_uuid'] else 'onvif:auto',
            'camera_user': door['camera_user'],
            'camera_pass': door['camera_pass'],
            'camera_path': door['camera_path'],
            'camera_port': door['camera_port'],
            'shelly': door['shelly_device_id'] or 'auto',
            'open_seconds': door['open_seconds'],
            'tokens': tokens,
        }
        if door['shelly_pass']:
            entry['shelly_pass'] = door['shelly_pass']
        config['doors'].append(entry)

    with open(output_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    return config
