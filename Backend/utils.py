import re
import base64
from datetime import datetime, timezone

def is_valid_svg(svg_string):
    if not svg_string or not isinstance(svg_string, str):
        return False
    svg_clean = re.sub(r'^\s*```(?:svg|xml)?\s*', '', svg_string.strip(), flags=re.IGNORECASE)
    svg_clean = re.sub(r'\s*```\s*$', '', svg_clean, flags=re.IGNORECASE)
    return svg_clean.lower().startswith('<svg') and svg_clean.endswith('>')