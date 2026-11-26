"""Initial GIF icon dimensions, shared by settings and the overlay."""


def apply_gif_size_default(visual, selected_path=None):
    """Promote an untouched legacy default, preserving deliberate sizes."""
    if visual.get('kill_icon_size_customized'):
        return False
    if (visual.get('kill_icon_width', 120), visual.get('kill_icon_height', 120)) != (120, 120):
        return False
    if selected_path is not None:
        paths = [selected_path]
    else:
        paths = [visual.get('kill_icon_path', '')]
        if visual.get('kill_icon_is_advanced'):
            icons = visual.get('kill_icons_1_5', [])
            if isinstance(icons, list):
                paths.extend(icon.get('path', '') for icon in icons[:5] if isinstance(icon, dict))
    if not any(isinstance(path, str) and path.lower().endswith('.gif') for path in paths):
        return False
    visual['kill_icon_width'] = visual['kill_icon_height'] = 320
    return True
