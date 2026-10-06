"""Mode ③: tick check and geometry calibration (implemented in P3)."""

from processors.idle import IdleProcessor


class TickProcessor(IdleProcessor):
    mode = "ticks"
    message = "刻度檢查將於 P3 實作"
