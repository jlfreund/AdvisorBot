"""Federal tax year 2026, IRS Revenue Procedure 2025-32, sections 4.01 and 4.03."""
STD_DEDUCTION = {True: 32200, False: 16100}
ORDINARY_JOINT = (24800, 100800, 211400, 403550, 512450, 768700)
ORDINARY_SINGLE = (12400, 50400, 105700, 201775, 256225, 640600)
GAINS_JOINT = (98900, 613700)
GAINS_SINGLE = (49450, 545500)
LOW_RATES = (0.10, 0.12, 0.22, 0.24, 0.32, 0.35)


def bracket_floors(brackets):
    return {"none": -1, "10": 0, **dict(zip(("12", "22", "24", "32", "35", "37"), brackets))}


FLOORS_JOINT = bracket_floors(ORDINARY_JOINT)
FLOORS_SINGLE = bracket_floors(ORDINARY_SINGLE)
