"""Owner-accepted withholding receipts; initially empty (ADR-020 v11).

Enrollment is a reviewed code change after acceptance of a frozen calibration
report. Keep receipts outside the implementation they pin, so enrollment does
not change its digest. Configuration alone cannot enroll a class.
"""

ACCEPTED_CALIBRATIONS: dict[str, dict[str, str]] = {}
