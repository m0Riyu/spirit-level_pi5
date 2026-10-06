"""Mode ②: camera alignment with AprilTags (implemented in P4)."""

from processors.idle import IdleProcessor


class AlignProcessor(IdleProcessor):
    mode = "align"
    message = "相機對位將於 P4 實作"
