"""The camera adapter, its motion detection and its classifier.

Deliberately re-exports nothing. Importing this package must not pull in OpenCV or an
inference runtime, so that a deployment running the radar path alone — or a test that never
touches a camera — does not pay for dependencies it has no use for. Import the module you
need directly:

    from sensors.adapters.camera.camera_adapter import CameraAdapter
    from sensors.adapters.camera.motion_detector import frame_difference

This package is the only place in the codebase that may name a video transport, a handset, or
an OpenCV primitive.
"""
