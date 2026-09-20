"""Seed the stand-in console with obviously-fake member data.

Every name here is invented. No real PII goes near this project -- the brief is
explicit about that, and a realistic-looking fake dataset is enough to exercise
the flow.

The ``flags`` column is what makes each member interesting. It is seeded now and
consumed by the exceptional-state handling in issue 3, so the trigger table and
the data stay in one place.
"""

from __future__ import annotations

from targetapp.data import DB_PATH, connect, init_schema

#: member_no, first, last, status, branch, flags
MEMBERS: list[tuple[str, str, str, str, str, str | None]] = [
    ("10001", "Dana", "Whitfield", "Active", "Main", None),
    ("10002", "Marcus", "Ellery", "Active", "Northgate", None),
    ("10003", "Priya", "Raghavan", "Active", "Main", "restricted"),
    ("10004", "Tomas", "Lindqvist", "Active", "Westfield", "notice"),
    ("10005", "Aisha", "Bello", "Under Review", "Main", "compliance_hold"),
    ("10006", "Grant", "Okonkwo", "Active", "Northgate", None),
    ("10007", "Nina", "Castellanos", "Active", "Main", None),
    ("10008", "Rafael", "Osei", "Active", "Westfield", None),
    ("10009", "Beatrix", "Lund", "Dormant", "Main", None),
    ("10010", "Hyun-woo", "Park", "Active", "Northgate", None),
]

#: member_no, acct_type, acct_number, balance_cents, opened_at
#:
#: 10006 deliberately has no Savings account: replay looking for a savings
#: balance finds the member but not the field, which is a different failure
#: from "no such member" and has to be reported differently.
ACCOUNTS: list[tuple[str, str, str, int, str]] = [
    ("10001", "Savings", "SV-4471902", 421033, "2019-03-14"),
    ("10001", "Checking", "CK-8820355", 128750, "2019-03-14"),
    ("10002", "Savings", "SV-4472884", 98214, "2021-07-02"),
    ("10002", "Checking", "CK-8821077", 45300, "2021-07-02"),
    ("10003", "Savings", "SV-4473119", 1560022, "2017-11-28"),
    ("10004", "Savings", "SV-4473640", 73915, "2020-01-09"),
    ("10004", "Checking", "CK-8822416", 21188, "2020-01-09"),
    ("10005", "Savings", "SV-4474203", 2204719, "2016-05-21"),
    ("10006", "Checking", "CK-8823990", 66240, "2022-09-30"),
    ("10007", "Savings", "SV-4475338", 310500, "2018-02-17"),
    ("10007", "Money Market", "MM-4475901", 7500000, "2018-02-17"),
    ("10008", "Savings", "SV-4476012", 14875, "2023-04-05"),
    ("10009", "Savings", "SV-4476644", 203, "2011-08-19"),
    ("10010", "Savings", "SV-4477188", 892340, "2015-12-01"),
    ("10010", "Checking", "CK-8825512", 173096, "2015-12-01"),
]


def seed(reset: bool = True) -> None:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect()
    init_schema(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO members"
        " (member_no, first, last, status, branch, flags) VALUES (?,?,?,?,?,?)",
        MEMBERS,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO accounts"
        " (member_no, acct_type, acct_number, balance_cents, opened_at)"
        " VALUES (?,?,?,?,?)",
        ACCOUNTS,
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed()
    print(f"seeded {len(MEMBERS)} members / {len(ACCOUNTS)} accounts -> {DB_PATH}")
