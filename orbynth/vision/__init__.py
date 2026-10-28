"""Seeing without a screen: the camera presence sense.

``camera`` is the only module here, and it is deliberately separate from
:mod:`orbynth.actions.vision`, which captures the *screen*. Room-facing capture has different
rules — consent, privacy, retention — and mixing the two would let a screen-capture permission
quietly become a camera permission.
"""
