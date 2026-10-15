"""Process-wide optional drawing enhancement; all platforms share one switch."""
# AI Phone internal master switch for drawing enhancement; not an ENV setting.
# False keeps JEV localization enabled but skips all enhancement collection.
VISUAL_ENHANCEMENT_ENABLED = True


def configure(*, visual_enhancement):
    """Enable or disable available drawing enhancements for every SDK instance."""
    if type(visual_enhancement) is not bool:
        raise TypeError('visual_enhancement must be a bool')
    global VISUAL_ENHANCEMENT_ENABLED
    VISUAL_ENHANCEMENT_ENABLED = visual_enhancement


def visual_enhancement_enabled():
    return VISUAL_ENHANCEMENT_ENABLED
