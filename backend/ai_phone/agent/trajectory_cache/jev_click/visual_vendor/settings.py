"""Process-wide optional drawing enhancement; all platforms share one switch."""
_visual_enhancement = False


def configure(*, visual_enhancement):
    """Enable or disable available drawing enhancements for every SDK instance."""
    if type(visual_enhancement) is not bool:
        raise TypeError('visual_enhancement must be a bool')
    global _visual_enhancement
    _visual_enhancement = visual_enhancement


def visual_enhancement_enabled():
    return _visual_enhancement
