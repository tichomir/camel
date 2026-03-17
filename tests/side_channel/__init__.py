"""Side-channel test suite for CaMeL Milestone 4.

Covers three attack classes:
- Indirect inference via loop count (M4-F1 STRICT mode closure)
- Exception-based bit leakage (M4-F6, M4-F9 redaction closure)
- Timing primitive exclusion (M4-F12 timing side-channel closure)

PRD Section 11 target: 100% pass rate for all implemented mitigations.
"""
