"""Live Android display dimensions over an existing UiAutomator connection."""


def live_window_size(device, fallback):
    try:
        info = device.info
        width, height = info['displayWidth'], info['displayHeight']
        if type(width) is not int or type(height) is not int or min(width, height) <= 0:
            raise ValueError('Invalid UiAutomator display dimensions')
        return width, height
    except Exception:
        return fallback()
