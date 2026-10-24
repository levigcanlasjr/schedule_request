"""Schedule Change Request app.

Run locally:  streamlit run app.py
Database:     set DATABASE_URL to your Neon connection string.
              Without it, the app uses a local SQLite file for testing.
First login:  set ADMIN_USERNAME and ADMIN_PASSWORD before the first run.
              Defaults are admin / change-me.
"""
import hashlib
import hmac
import os
import secrets
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# ---------- Settings ----------
START = date(2026, 10, 24)
END = date(2026, 11, 6)
# End of the first schedule period. Anyone complete up to this date still counts as Submitted.
FIRST_END = date(2026, 11, 3)
DAYS = [START + timedelta(days=i) for i in range((END - START).days + 1)]

AM = "5:00 AM - 2:00 PM"
PM = "2:00 PM - 11:00 PM"
OFF = "OFF"
SHIFTS = [AM, PM, OFF]

ROLES = ["Employee", "Group Leader", "Approver", "Admin"]
# Roles that submit schedules and are counted in totals
COUNTED = "('Employee', 'Group Leader')"
GROUPS = ["HGT", "HPT", "HID"]  # groups that hold employees and group leaders
ALL_GROUPS = GROUPS + ["HiEx"]  # HiEx is for approvers and admin
LOCAL_TZ = timezone(timedelta(hours=8))

APP_NAME = "Team Schedule"
st.set_page_config(page_title=APP_NAME, layout="wide", initial_sidebar_state="collapsed")

# Arrivals run on their own date range
ARR_START = date(2026, 10, 25)
ARR_END = date(2026, 11, 5)
ARR_DAYS = [ARR_START + timedelta(days=i) for i in range((ARR_END - ARR_START).days + 1)]
TIMES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]  # stored as 24-hour text


def time_label(t):
    return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea, select {
    font-family: 'Inter', sans-serif;
}
.stApp { background: #f5f7fb; }
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stSidebarCollapsedControl"] { visibility: hidden; }
.block-container { padding-top: 1.5rem; max-width: 1200px; }
h1, h2, h3 { color: #111827; font-weight: 700; letter-spacing: -0.01em; }
[data-testid="stVerticalBlockBorderWrapper"] {
    background: #ffffff; border-radius: 14px; border: 1px solid #e5e7eb !important;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
.card-title { font-size: 1.05rem; font-weight: 600; color: #111827; margin: 0.25rem 0 0.15rem 0; }
.card-text { font-size: 0.88rem; color: #6b7280; min-height: 2.6rem; margin-bottom: 0.5rem; }
.topbar { display: flex; align-items: center; justify-content: space-between; }
.app-name { font-size: 1.35rem; font-weight: 700; color: #111827; }
.who { font-size: 0.9rem; color: #6b7280; }
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; background: #eef2ff;
        color: #4338ca; font-size: 0.78rem; font-weight: 600; margin-left: 6px; }
.stButton button { border-radius: 10px; font-weight: 500; }
.stButton button[kind="primary"] { background: #4f46e5; border-color: #4f46e5; }
[data-testid="stMetric"] { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 10px 14px; }
[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
.login-title { font-size: 1.8rem; font-weight: 700; color: #111827; text-align: center; margin-top: 3rem; }
.login-sub { color: #6b7280; text-align: center; margin-bottom: 1.2rem; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------- Database ----------
@st.cache_resource
def engine():
    url = os.environ.get("DATABASE_URL", "sqlite:///shift_requests.db")
    if url.startswith("postgres"):  # accept any Postgres URL form and use the psycopg 3 driver
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    return create_engine(url, pool_pre_ping=True)


def run(sql, **params):
    with engine().begin() as conn:
        conn.execute(text(sql), params)


def rows(sql, **params):
    with engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def check_password(password, stored):
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


@st.cache_resource
def init_db():
    run(
        """CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL)"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS schedule (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            shift TEXT NOT NULL,
            PRIMARY KEY (username, day))"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS requests (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            requested_shift TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_by TEXT,
            PRIMARY KEY (username, day))"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS attendance (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            status TEXT NOT NULL,
            marked_by TEXT,
            PRIMARY KEY (username, day))"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS arrivals (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            airport_time TEXT NOT NULL,
            hotel_time TEXT NOT NULL,
            delegates INTEGER NOT NULL,
            entered_by TEXT,
            entered_at TEXT)"""
    )
    try:  # adds who confirmed an attendance mark; marks made before this were made by leaders
        run("ALTER TABLE attendance ADD COLUMN confirmed_by TEXT")
        run("UPDATE attendance SET confirmed_by = marked_by")
    except Exception:
        pass
    try:  # adds the time a request was approved or rejected
        run("ALTER TABLE requests ADD COLUMN decided_at TEXT")
    except Exception:
        pass
    try:  # adds the group column to a database made by the first version
        run("ALTER TABLE users ADD COLUMN team TEXT")
    except Exception:
        pass
    try:  # adds the forced password change flag; existing non-admin users get flagged
        run("ALTER TABLE users ADD COLUMN must_change INTEGER DEFAULT 1")
        run("UPDATE users SET must_change = 0 WHERE role = 'Admin'")
    except Exception:
        pass
    if not rows("SELECT username FROM users LIMIT 1"):
        run(
            "INSERT INTO users (username, name, password_hash, role, must_change) "
            "VALUES (:u, :n, :p, 'Admin', 0)",
            u=os.environ.get("ADMIN_USERNAME", "admin"),
            n="Admin",
            p=hash_password(os.environ.get("ADMIN_PASSWORD", "change-me")),
        )
    return True


# ---------- Shared pieces ----------
def now_local():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def decision_note(status, by_name, at):
    """Example: Approved by Maria Cruz, Oct 17 9:30 PM"""
    note = f"{status} by {by_name or 'unknown'}"
    if at:
        try:
            note += ", " + datetime.strptime(at, "%Y-%m-%d %H:%M").strftime("%b %d %I:%M %p").replace(" 0", " ")
        except ValueError:
            pass
    return note


def group_error(role, team):
    if role in ("Employee", "Group Leader") and team not in GROUPS:
        return f"{role} must be in {', '.join(GROUPS)}."
    if team and team not in ALL_GROUPS:
        return f"Group {team} is not valid."
    return None


def today_local():
    return datetime.now(LOCAL_TZ).date()


def load_attendance(where, params):
    """Returns {(username, day): row} with status, marked_by, confirmed_by and the confirmer's name."""
    return {
        (r["username"], r["day"]): r
        for r in rows(
            "SELECT a.username, a.day, a.status, a.marked_by, a.confirmed_by, c.name AS confirmed_name "
            "FROM attendance a JOIN users u ON u.username = a.username "
            f"LEFT JOIN users c ON c.username = a.confirmed_by WHERE {where}",
            **params,
        )
    }


def attendance_label(a, owner):
    if not a:
        return "Not marked"
    if a["confirmed_by"]:
        return f'{a["status"]} (confirmed by {a["confirmed_name"] or "unknown"})'
    if a["marked_by"] == owner:
        return f'Self-marked: {a["status"]}'
    return f'{a["status"]} (not confirmed)'


def day_label(iso):
    return date.fromisoformat(iso).strftime("%a, %b %d")


def summary_table(team=None):
    """Counts Employees only. Admins and Approvers are left out."""
    who = f"u.role IN {COUNTED}" + (" AND u.team = :t" if team else "")
    params = {"t": team} if team else {}
    total = rows(f"SELECT COUNT(*) AS n FROM users u WHERE {who}", **params)[0]["n"]
    sched = rows(
        "SELECT s.day, s.shift, COUNT(*) AS n FROM schedule s "
        f"JOIN users u ON u.username = s.username WHERE {who} GROUP BY s.day, s.shift",
        **params,
    )
    reqs = rows(
        "SELECT r.day, r.requested_shift, COUNT(*) AS n FROM requests r "
        f"JOIN users u ON u.username = r.username WHERE {who} AND r.status = 'Pending' "
        "GROUP BY r.day, r.requested_shift",
        **params,
    )
    s = {(r["day"], r["shift"]): r["n"] for r in sched}
    q = {(r["day"], r["requested_shift"]): r["n"] for r in reqs}
    data = []
    for d in DAYS:
        k = d.isoformat()
        counts = [s.get((k, sh), 0) for sh in SHIFTS]
        asked = [q.get((k, sh), 0) for sh in SHIFTS]
        data.append(
            {
                "Date": day_label(k),
                AM: counts[0],
                PM: counts[1],
                OFF: counts[2],
                "Not set": total - sum(counts),
                "Change requests": sum(asked),
                "Asking for 5 AM": asked[0],
                "Asking for 2 PM": asked[1],
                "Asking for OFF": asked[2],
            }
        )
    return pd.DataFrame(data)


def show_summary():
    tabs = st.tabs(["All groups"] + GROUPS)
    for tab, team in zip(tabs, [None] + GROUPS):
        with tab:
            st.dataframe(summary_table(team), hide_index=True)
    st.caption(
        "Counts employees and group leaders only. Change requests counts pending requests only. "
        "An employee with no group shows under All groups only."
    )


# ---------- Pages ----------
def login_page():
    left, mid, right = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(f'<div class="login-title">{APP_NAME}</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-sub">Sign in to continue</div>', unsafe_allow_html=True)
        login_form()


def login_form():
    with st.form("login", border=True):
        username = st.text_input("Username").strip().lower()
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in", type="primary", use_container_width=True):
            found = rows("SELECT * FROM users WHERE lower(username) = :u", u=username)
            if found and check_password(password, found[0]["password_hash"]):
                st.session_state.user = {
                    "username": found[0]["username"],
                    "name": found[0]["name"],
                    "role": found[0]["role"],
                    "team": found[0]["team"],
                    "must_change": bool(found[0]["must_change"]),
                }
                st.rerun()
            st.error("Wrong username or password.")


def change_password_page(user, forced=False):
    st.header("Change Password")
    if forced:
        st.info("You are using a temporary password. Set your own password to continue.")
    with st.form("change_password", clear_on_submit=True):
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        again = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Change password"):
            found = rows("SELECT password_hash FROM users WHERE username = :u", u=user["username"])
            if not found or not check_password(current, found[0]["password_hash"]):
                st.error("Current password is wrong.")
            elif len(new) < 8:
                st.error("New password must be at least 8 characters.")
            elif new != again:
                st.error("The new passwords do not match.")
            elif new == current:
                st.error("New password must be different from the current one.")
            else:
                run(
                    "UPDATE users SET password_hash = :p, must_change = 0 WHERE username = :u",
                    p=hash_password(new), u=user["username"],
                )
                st.session_state.user["must_change"] = False
                st.session_state.password_changed = True
                st.rerun()
    if st.session_state.pop("password_changed", False):
        st.success("Password changed.")


def my_schedule_page(user):
    st.header("My Schedule")
    u = user["username"]
    sched = {r["day"]: r["shift"] for r in rows("SELECT day, shift FROM schedule WHERE username = :u", u=u)}
    reqs = {
        r["day"]: r
        for r in rows(
            "SELECT r.*, d.name AS decided_name FROM requests r "
            "LEFT JOIN users d ON d.username = r.decided_by WHERE r.username = :u",
            u=u,
        )
    }

    st.caption(
        "Set your actual schedule first. Once saved, it is locked. "
        "To change it, pick a shift under Request and wait for approval."
    )

    new_actual, new_request, pending_now = {}, {}, {}
    with st.form("mine"):
        h = st.columns([2, 3, 3, 3])
        h[0].markdown("**Date**")
        h[1].markdown("**Actual schedule**")
        h[2].markdown("**Request**")
        h[3].markdown("**Request status**")
        for d in DAYS:
            k = d.isoformat()
            c = st.columns([2, 3, 3, 3])
            c[0].write(day_label(k))
            actual = sched.get(k)
            if actual:
                c[1].write(actual)
            else:
                new_actual[k] = c[1].selectbox(
                    "Actual", ["Not set"] + SHIFTS, key=f"a_{k}", label_visibility="collapsed"
                )
            r = reqs.get(k)
            pending = r["requested_shift"] if r and r["status"] == "Pending" else None
            if actual:
                opts = ["No request"] + [s for s in SHIFTS if s != actual]
                new_request[k] = c[2].selectbox(
                    "Request",
                    opts,
                    index=opts.index(pending) if pending in opts else 0,
                    key=f"r_{k}_{actual}",
                    label_visibility="collapsed",
                )
                pending_now[k] = pending
            else:
                c[2].caption("Set actual schedule first")
            if r and r["status"] in ("Approved", "Rejected"):
                c[3].write(f'{r["requested_shift"]}: {decision_note(r["status"], r["decided_name"], r["decided_at"])}')
            elif r:
                c[3].write(f'{r["status"]}: {r["requested_shift"]}')
        saved = st.form_submit_button("Save")

    if saved:
        for k, shift in new_actual.items():
            if shift != "Not set":
                run(
                    "INSERT INTO schedule (username, day, shift) VALUES (:u, :d, :s) "
                    "ON CONFLICT (username, day) DO NOTHING",
                    u=u, d=k, s=shift,
                )
        for k, choice in new_request.items():
            if choice == "No request":
                if pending_now[k]:
                    run(
                        "DELETE FROM requests WHERE username = :u AND day = :d AND status = 'Pending'",
                        u=u, d=k,
                    )
            elif choice != pending_now[k]:
                run(
                    "INSERT INTO requests (username, day, requested_shift, status, decided_by) "
                    "VALUES (:u, :d, :s, 'Pending', NULL) "
                    "ON CONFLICT (username, day) DO UPDATE SET "
                    "requested_shift = :s, status = 'Pending', decided_by = NULL, decided_at = NULL",
                    u=u, d=k, s=choice,
                )
        st.rerun()

    st.subheader("Team totals")
    show_summary()


def my_attendance_section(user):
    u = user["username"]
    today = today_local()
    days = [d for d in (today - timedelta(days=1), today) if d in DAYS]
    st.subheader("My attendance")
    if not days:
        st.caption("Attendance opens on the schedule dates, for yesterday and today only.")
        return
    shifts = {r["day"]: r["shift"] for r in rows("SELECT day, shift FROM schedule WHERE username = :u", u=u)}
    att = load_attendance("a.username = :u", {"u": u})
    leader = user["role"] == "Group Leader"
    if leader:
        st.caption("Mark your attendance for yesterday and today. As a group leader, your mark is confirmed right away.")
    else:
        st.caption("Mark your attendance for yesterday and today. Your group leader or an approver will confirm it.")
    picks = {}
    with st.form("my_attendance"):
        for d in days:
            k = d.isoformat()
            c = st.columns([2, 3, 3])
            c[0].write(day_label(k))
            sh = shifts.get(k)
            if sh not in (AM, PM):
                c[1].write(sh or "Not set")
                c[2].caption("No shift to mark")
                continue
            a = att.get((u, k))
            c[1].write(f"{sh}  \n{attendance_label(a, u)}")
            if a and a["confirmed_by"] and not leader:
                c[2].caption("Confirmed. Ask your group leader to change it.")
                continue
            cur = a["status"] if a else "Not marked"
            picks[k] = c[2].selectbox(
                "Attendance", ATTENDANCE, index=ATTENDANCE.index(cur),
                key=f"self_att_{k}", label_visibility="collapsed",
            )
        saved = st.form_submit_button("Save my attendance")
    if saved:
        for k, new in picks.items():
            a = att.get((u, k))
            if new == "Not marked":
                if a:
                    run(
                        "DELETE FROM attendance WHERE username = :u AND day = :d"
                        + ("" if leader else " AND confirmed_by IS NULL"),
                        u=u, d=k,
                    )
            elif not a or a["status"] != new or a["marked_by"] != u or (leader and not a["confirmed_by"]):
                run(
                    "INSERT INTO attendance (username, day, status, marked_by, confirmed_by) "
                    "VALUES (:u, :d, :s, :u, :c) "
                    "ON CONFLICT (username, day) DO UPDATE SET status = :s, marked_by = :u, confirmed_by = :c",
                    u=u, d=k, s=new, c=u if leader else None,
                )
        st.rerun()


def can_decide(user, request_owner, owner_team):
    if user["role"] in ("Approver", "Admin"):
        return True
    if user["role"] == "Group Leader":
        return owner_team == user["team"] and request_owner != user["username"]
    return False


def approvals_page(user):
    st.header("Approvals")
    leader = user["role"] == "Group Leader"
    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')
        st.dataframe(summary_table(user["team"]), hide_index=True)
    else:
        show_summary()

    params = {}
    team_filter = ""
    if leader:
        team_filter = " AND u.team = :t"
        params["t"] = user["team"]

    st.subheader("Pending requests")
    pending = rows(
        "SELECT r.username, u.name, u.team, r.day, r.requested_shift, s.shift AS actual "
        "FROM requests r "
        "JOIN users u ON u.username = r.username "
        "LEFT JOIN schedule s ON s.username = r.username AND s.day = r.day "
        f"WHERE r.status = 'Pending' AND u.role IN {COUNTED}{team_filter} ORDER BY r.day, u.team, u.name",
        **params,
    )
    if not pending:
        st.write("No pending requests.")
    else:
        if leader:
            st.caption("Your own requests go to an approver.")
        widths = [3, 1, 2, 3, 3, 1, 1]
        h = st.columns(widths)
        for col, label in zip(h, ["Name", "Group", "Date", "Actual", "Requested", "", ""]):
            col.markdown(f"**{label}**")
        for p in pending:
            key = f'{p["username"]}_{p["day"]}'
            c = st.columns(widths)
            c[0].write(p["name"])
            c[1].write(p["team"] or "")
            c[2].write(day_label(p["day"]))
            c[3].write(p["actual"] or "Not set")
            c[4].write(p["requested_shift"])
            if not can_decide(user, p["username"], p["team"]):
                c[5].caption("Needs an approver")
                continue
            if c[5].button("Approve", key=f"ok_{key}"):
                with engine().begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO schedule (username, day, shift) VALUES (:u, :d, :s) "
                            "ON CONFLICT (username, day) DO UPDATE SET shift = :s"
                        ),
                        {"u": p["username"], "d": p["day"], "s": p["requested_shift"]},
                    )
                    conn.execute(
                        text(
                            "UPDATE requests SET status = 'Approved', decided_by = :by, decided_at = :at "
                            "WHERE username = :u AND day = :d"
                        ),
                        {"by": user["username"], "at": now_local(), "u": p["username"], "d": p["day"]},
                    )
                st.rerun()
            if c[6].button("Reject", key=f"no_{key}"):
                run(
                    "UPDATE requests SET status = 'Rejected', decided_by = :by, decided_at = :at "
                    "WHERE username = :u AND day = :d",
                    by=user["username"], at=now_local(), u=p["username"], d=p["day"],
                )
                st.rerun()

    st.subheader("Recent decisions")
    decided = rows(
        "SELECT u.name, u.team, r.day, r.requested_shift, r.status, r.decided_at, d.name AS decided_name "
        "FROM requests r JOIN users u ON u.username = r.username "
        "LEFT JOIN users d ON d.username = r.decided_by "
        f"WHERE r.status IN ('Approved', 'Rejected'){team_filter} "
        "ORDER BY r.decided_at DESC LIMIT 100",
        **params,
    )
    if not decided:
        st.write("No decisions yet.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Name": d["name"],
                        "Group": d["team"] or "",
                        "Date": day_label(d["day"]),
                        "Requested": d["requested_shift"],
                        "Decision": decision_note(d["status"], d["decided_name"], d["decided_at"]),
                    }
                    for d in decided
                ]
            ),
            hide_index=True,
        )


SHORT = {AM: "5AM-2PM", PM: "2PM-11PM", OFF: "OFF"}


def all_schedules_page(user):
    st.header("Schedule")
    leader = user["role"] == "Group Leader"
    params = {}
    where = f"u.role IN {COUNTED}"
    if leader:
        where += " AND u.team = :t"
        params["t"] = user["team"]
    people = rows(f"SELECT u.username, u.name, u.team FROM users u WHERE {where} ORDER BY u.team, u.name", **params)
    sched = {
        (r["username"], r["day"]): r["shift"]
        for r in rows(f"SELECT s.username, s.day, s.shift FROM schedule s JOIN users u ON u.username = s.username WHERE {where}", **params)
    }
    pend = {
        (r["username"], r["day"]): r["requested_shift"]
        for r in rows(
            "SELECT r.username, r.day, r.requested_shift FROM requests r JOIN users u ON u.username = r.username "
            f"WHERE {where} AND r.status = 'Pending'",
            **params,
        )
    }

    approved = {
        (r["username"], r["day"]): r["decided_name"]
        for r in rows(
            "SELECT r.username, r.day, d.name AS decided_name FROM requests r "
            "JOIN users u ON u.username = r.username LEFT JOIN users d ON d.username = r.decided_by "
            f"WHERE {where} AND r.status = 'Approved'",
            **params,
        )
    }
    data = []
    for p in people:
        for d in DAYS:
            k = d.isoformat()
            note = ""
            if (p["username"], k) in pend:
                note = f'Pending request: {pend[(p["username"], k)]}'
            elif (p["username"], k) in approved:
                note = f'Change approved by {approved[(p["username"], k)] or "unknown"}'
            data.append(
                {
                    "Date": d,
                    "Employee": p["name"],
                    "Shift": sched.get((p["username"], k), "Not set"),
                    "Group": p["team"] or "",
                    "Note": note,
                }
            )
    df = pd.DataFrame(data, columns=["Date", "Employee", "Shift", "Group", "Note"])

    f = st.columns(3)
    if leader:
        f[0].text_input("Group", value=user["team"] or "No group", disabled=True)
    else:
        team = f[0].selectbox("Group", ["All groups"] + GROUPS, key="all_sched_group")
        if team != "All groups":
            df = df[df["Group"] == team]
    day_pick = f[1].selectbox(
        "Date", ["All dates"] + DAYS, key="sched_day",
        format_func=lambda d: d if isinstance(d, str) else d.strftime("%a, %b %d"),
    )
    if day_pick != "All dates":
        df = df[df["Date"] == day_pick]
    shift_pick = f[2].selectbox("Shift", ["All shifts"] + SHIFTS + ["Not set"], key="sched_shift")
    if shift_pick != "All shifts":
        df = df[df["Shift"] == shift_pick]

    order = {g: i for i, g in enumerate(ALL_GROUPS)}
    df = df.assign(_g=df["Group"].map(lambda g: order.get(g, 99))).sort_values(["_g", "Date", "Employee"]).drop(columns="_g")
    df["Date"] = df["Date"].map(lambda d: d.strftime("%a, %b %d"))
    st.caption(f"{len(df)} rows. Sorted by group, then date, then employee.")
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.download_button(
        "Download this table (CSV)", df.to_csv(index=False), file_name="schedule.csv", mime="text/csv"
    )

    if user["role"] != "Admin" or not people:
        return

    st.subheader("Edit an employee's schedule")
    st.caption(
        "Changes to the actual schedule save right away and skip approval. "
        "Pending requests stay open for an approver. You can also add or remove a request for this person."
    )
    names = {p["username"]: p for p in people}
    target = st.selectbox(
        "Employee",
        list(names),
        format_func=lambda x: f'{names[x]["name"]} ({names[x]["team"] or "no group"})',
        key="edit_sched_target",
    )
    opts_actual = ["Not set"] + SHIFTS
    opts_req = ["No request"] + SHIFTS
    new_actual, new_req = {}, {}
    with st.form(f"admin_sched_{target}"):
        h = st.columns([2, 3, 3])
        h[0].markdown("**Date**")
        h[1].markdown("**Actual schedule**")
        h[2].markdown("**Pending request**")
        for d in DAYS:
            k = d.isoformat()
            c = st.columns([2, 3, 3])
            c[0].write(day_label(k))
            cur = sched.get((target, k))
            new_actual[k] = c[1].selectbox(
                "Actual", opts_actual, index=opts_actual.index(cur) if cur in opts_actual else 0,
                key=f"aa_{target}_{k}", label_visibility="collapsed",
            )
            curr = pend.get((target, k))
            new_req[k] = c[2].selectbox(
                "Request", opts_req, index=opts_req.index(curr) if curr in opts_req else 0,
                key=f"ar_{target}_{k}", label_visibility="collapsed",
            )
        saved = st.form_submit_button("Save changes")

    if saved:
        changed = 0
        for k in new_actual:
            cur, new = sched.get((target, k)), new_actual[k]
            if new == "Not set" and cur:
                run("DELETE FROM schedule WHERE username = :u AND day = :d", u=target, d=k)
                changed += 1
            elif new != "Not set" and new != cur:
                run(
                    "INSERT INTO schedule (username, day, shift) VALUES (:u, :d, :s) "
                    "ON CONFLICT (username, day) DO UPDATE SET shift = :s",
                    u=target, d=k, s=new,
                )
                changed += 1
            curr, newr = pend.get((target, k)), new_req[k]
            if newr == curr or (newr == "No request" and not curr):
                continue
            if newr == "No request":
                run(
                    "DELETE FROM requests WHERE username = :u AND day = :d AND status = 'Pending'",
                    u=target, d=k,
                )
            else:
                run(
                    "INSERT INTO requests (username, day, requested_shift, status, decided_by) "
                    "VALUES (:u, :d, :s, 'Pending', NULL) "
                    "ON CONFLICT (username, day) DO UPDATE SET "
                    "requested_shift = :s, status = 'Pending', decided_by = NULL, decided_at = NULL",
                    u=target, d=k, s=newr,
                )
            changed += 1
        st.session_state.admin_sched_msg = f"Saved {changed} change(s) for {names[target]['name']}."
        st.rerun()
    if "admin_sched_msg" in st.session_state:
        st.success(st.session_state.pop("admin_sched_msg"))


ATTENDANCE = ["Not marked", "Present", "Late", "Absent", "On leave"]


def by_day_page(user):
    st.header("By Day")
    leader = user["role"] == "Group Leader"
    params = {}
    where = f"u.role IN {COUNTED}"
    if leader:
        where += " AND u.team = :t"
        params["t"] = user["team"]
    people = rows(f"SELECT u.username, u.name, u.team FROM users u WHERE {where} ORDER BY u.name", **params)
    sched = {
        (r["username"], r["day"]): r["shift"]
        for r in rows(f"SELECT s.username, s.day, s.shift FROM schedule s JOIN users u ON u.username = s.username WHERE {where}", **params)
    }
    pend = {
        (r["username"], r["day"]): r["requested_shift"]
        for r in rows(
            "SELECT r.username, r.day, r.requested_shift FROM requests r JOIN users u ON u.username = r.username "
            f"WHERE {where} AND r.status = 'Pending'",
            **params,
        )
    }
    att_rows = load_attendance(where, params)
    att = {key: r["status"] for key, r in att_rows.items()}

    groups = [user["team"]] if leader else GROUPS + (["No group"] if any(not p["team"] for p in people) else [])
    def members(g):
        return [p for p in people if (p["team"] or "No group") == g]

    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')

    # ---------- One day ----------
    today = today_local()
    default = DAYS.index(today) if today in DAYS else 0
    day = st.selectbox("Date", DAYS, index=default, format_func=lambda d: d.strftime("%a, %b %d"))
    k = day.isoformat()
    export = []
    for g in groups:
        team_people = members(g)
        st.subheader(f"{g} ({len(team_people)})")
        by_shift = {sh: [] for sh in SHIFTS + ["Not set"]}
        for p in team_people:
            by_shift[sched.get((p["username"], k), "Not set")].append(p)
        cols = st.columns(4)
        for col, sh in zip(cols, SHIFTS + ["Not set"]):
            col.metric(sh, len(by_shift[sh]))

        with st.form(f"att_{g}_{k}"):
            picks = {}
            for sh in [AM, PM]:
                on_shift = by_shift[sh]
                marked = [att.get((p["username"], k), "Not marked") for p in on_shift]
                tally = ", ".join(f"{m} {marked.count(m)}" for m in ATTENDANCE[1:] if marked.count(m))
                waiting = sum(
                    1 for p in on_shift
                    if att_rows.get((p["username"], k)) and not att_rows[(p["username"], k)]["confirmed_by"]
                )
                if waiting:
                    tally += f"{'; ' if tally else ''}{waiting} not confirmed"
                st.markdown(f"**{sh}** ({len(on_shift)} scheduled{'; ' + tally if tally else ''})")
                if not on_shift:
                    st.caption("No one scheduled.")
                for p in on_shift:
                    label = p["name"]
                    if (p["username"], k) in pend:
                        label += f' (req: {SHORT[pend[(p["username"], k)]]})'
                    c = st.columns([3, 3, 2, 1])
                    c[0].write(label)
                    a = att_rows.get((p["username"], k))
                    c[1].caption(attendance_label(a, p["username"]))
                    cur = a["status"] if a else "Not marked"
                    own = False  # group leaders mark and confirm their own row
                    new = c[2].selectbox(
                        "Attendance", ATTENDANCE, index=ATTENDANCE.index(cur),
                        key=f"att_{p['username']}_{k}", label_visibility="collapsed",
                        disabled=own,
                    )
                    ok = c[3].checkbox(
                        "Confirm", value=bool(a and a["confirmed_by"]),
                        key=f"conf_{p['username']}_{k}", disabled=own,
                    )
                    if not own:
                        picks[p["username"]] = (new, ok)
            for sh in [OFF, "Not set"]:
                names = []
                for p in by_shift[sh]:
                    label = p["name"]
                    if (p["username"], k) in pend:
                        label += f' (req: {SHORT[pend[(p["username"], k)]]})'
                    names.append(label)
                st.markdown(f"**{sh}** ({len(names)}): " + (", ".join(names) if names else "none"))
            save = st.form_submit_button(f"Save attendance for {g}")
        if save:
            for u, (new, ok) in picks.items():
                a = att_rows.get((u, k))
                if new == "Not marked":
                    if a:
                        run("DELETE FROM attendance WHERE username = :u AND day = :d", u=u, d=k)
                    continue
                changed = not a or a["status"] != new
                confirm_by = user["username"] if (ok or changed) else None
                if not changed and bool(a["confirmed_by"]) == bool(confirm_by):
                    continue
                run(
                    "INSERT INTO attendance (username, day, status, marked_by, confirmed_by) "
                    "VALUES (:u, :d, :s, :by, :c) "
                    "ON CONFLICT (username, day) DO UPDATE SET status = :s, confirmed_by = :c"
                    + (", marked_by = :by" if changed else ""),
                    u=u, d=k, s=new, by=user["username"], c=confirm_by,
                )
            st.session_state.att_msg = f"Attendance saved for {g}."
            st.rerun()

        for sh in SHIFTS + ["Not set"]:
            for p in by_shift[sh]:
                export.append(
                    {
                        "Date": day_label(k),
                        "Group": g,
                        "Name": p["name"],
                        "Shift": sh,
                        "Pending request": pend.get((p["username"], k), ""),
                        "Attendance": attendance_label(att_rows.get((p["username"], k)), p["username"]) if sh in (AM, PM) else "",
                    }
                )
    if "att_msg" in st.session_state:
        st.success(st.session_state.pop("att_msg"))
    st.download_button(
        "Download this day (CSV)",
        pd.DataFrame(export).to_csv(index=False),
        file_name=f"schedule_{k}.csv",
        mime="text/csv",
    )

    # ---------- Full view ----------
    st.subheader("Full view (all days)")
    st.caption("Shift counts per group per day. Attendance counts people on the 5 AM and 2 PM shifts. "
        "Not confirmed counts marks that a group leader or approver has not confirmed yet.")
    full = []
    for d in DAYS:
        dk = d.isoformat()
        for g in groups:
            team_people = members(g)
            shifts = [sched.get((p["username"], dk), "Not set") for p in team_people]
            marks = [att.get((p["username"], dk), "Not marked") for p, sh in zip(team_people, shifts) if sh in (AM, PM)]
            line = {"Date": d.strftime("%a %b %d"), "Group": g}
            for sh in SHIFTS + ["Not set"]:
                line[SHORT.get(sh, sh)] = shifts.count(sh)
            for m in ATTENDANCE[1:] + ["Not marked"]:
                line[m] = marks.count(m)
            line["Not confirmed"] = sum(
                1 for p, sh in zip(team_people, shifts)
                if sh in (AM, PM) and att_rows.get((p["username"], dk)) and not att_rows[(p["username"], dk)]["confirmed_by"]
            )
            full.append(line)
    full_df = pd.DataFrame(full)
    if not leader and len(groups) > 1:
        pick = st.selectbox("Group", ["All groups"] + groups, key="full_group")
        if pick != "All groups":
            full_df = full_df[full_df["Group"] == pick]
    st.dataframe(full_df, hide_index=True)
    st.download_button(
        "Download full view (CSV)", full_df.to_csv(index=False), file_name="full_view.csv", mime="text/csv"
    )


def shift_summary_page(user):
    st.header("Shift Summary")
    leader = user["role"] == "Group Leader"
    if leader:
        g = user["team"]
        st.caption(f'Showing your group only: {g or "no group set"}')
    else:
        g = st.selectbox("Group", ["All groups"] + GROUPS, key="sum_group")
    params, where = {}, f"u.role IN {COUNTED}"
    if g != "All groups":
        where += " AND u.team = :t"
        params["t"] = g
    people = [r["username"] for r in rows(f"SELECT u.username FROM users u WHERE {where}", **params)]
    sched = {
        (r["username"], r["day"]): r["shift"]
        for r in rows(f"SELECT s.username, s.day, s.shift FROM schedule s JOIN users u ON u.username = s.username WHERE {where}", **params)
    }
    att = load_attendance(where, params)

    statuses = ATTENDANCE[1:] + ["Not marked"]
    table = []
    for d in DAYS:
        k = d.isoformat()
        shifts = {u: sched.get((u, k), "Not set") for u in people}
        for sh in (AM, PM):
            on = [u for u in people if shifts[u] == sh]
            marks = [att[(u, k)]["status"] if (u, k) in att else "Not marked" for u in on]
            line = {"Date": d.strftime("%a, %b %d"), "Shift": sh, "Scheduled": len(on)}
            for m in statuses:
                line[m] = marks.count(m)
            line["Not confirmed"] = sum(1 for u in on if (u, k) in att and not att[(u, k)]["confirmed_by"])
            table.append(line)
        for sh in (OFF, "Not set"):
            table.append({"Date": d.strftime("%a, %b %d"), "Shift": sh, "Scheduled": sum(1 for u in people if shifts[u] == sh)})
    cols = ["Date", "Shift", "Scheduled"] + statuses + ["Not confirmed"]
    df = pd.DataFrame(table, columns=cols)
    for c in statuses + ["Not confirmed"]:
        df[c] = df[c].astype("Int64")

    day_pick = st.selectbox(
        "Date", ["All dates"] + [d.strftime("%a, %b %d") for d in DAYS], key="sum_day",
    )
    if day_pick != "All dates":
        df = df[df["Date"] == day_pick]
    st.caption(
        f"{len(people)} employees and group leaders. Present, Late, Absent, On leave and Not marked add up to "
        "Scheduled for each shift. Not confirmed is part of those counts. Attendance is blank for OFF and Not set."
    )
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.download_button(
        "Download summary (CSV)", df.to_csv(index=False),
        file_name=f"shift_summary_{g}.csv".replace(" ", "_"), mime="text/csv",
    )


REVIEWERS = ("Group Leader", "Approver", "Admin")


def attendance_page(user):
    st.header("Attendance")
    if user["role"] in ("Employee", "Group Leader"):
        with st.container(border=True):
            my_attendance_section(user)
    if user["role"] in REVIEWERS:
        with st.container(border=True):
            attendance_review(user)


def attendance_review(user):
    st.subheader("Team attendance")
    leader = user["role"] == "Group Leader"
    f = st.columns(2)
    today = today_local()
    day = f[0].selectbox(
        "Date", DAYS, index=DAYS.index(today) if today in DAYS else 0,
        format_func=lambda d: d.strftime("%a, %b %d"), key="att_rev_day",
    )
    if leader:
        team = user["team"]
        f[1].text_input("Group", value=team or "No group", disabled=True)
    else:
        team = f[1].selectbox("Group", ["All groups"] + GROUPS, key="att_rev_group")
    k = day.isoformat()

    params, where = {}, f"u.role IN {COUNTED}"
    if team != "All groups":
        where += " AND u.team = :t"
        params["t"] = team
    people = rows(f"SELECT u.username, u.name, u.team FROM users u WHERE {where} ORDER BY u.team, u.name", **params)
    shifts = {
        r["username"]: r["shift"]
        for r in rows(
            f"SELECT s.username, s.shift FROM schedule s JOIN users u ON u.username = s.username WHERE {where} AND s.day = :d",
            d=k, **params,
        )
    }
    att = load_attendance(where + " AND a.day = :d", dict(params, d=k))

    # Overview per group
    overview = []
    for g in sorted({p["team"] or "No group" for p in people}, key=lambda x: ALL_GROUPS.index(x) if x in ALL_GROUPS else 99):
        on = [p["username"] for p in people if (p["team"] or "No group") == g and shifts.get(p["username"]) in (AM, PM)]
        marks = [att[(u, k)]["status"] if (u, k) in att else "Not marked" for u in on]
        line = {"Group": g, "Scheduled": len(on)}
        for m in ATTENDANCE[1:] + ["Not marked"]:
            line[m] = marks.count(m)
        line["Not confirmed"] = sum(1 for u in on if (u, k) in att and not att[(u, k)]["confirmed_by"])
        overview.append(line)
    if overview:
        ov = pd.DataFrame(overview)
        if len(ov) > 1:
            total = ov.drop(columns="Group").sum()
            ov = pd.concat([ov, pd.DataFrame([{"Group": "Overall", **total.to_dict()}])], ignore_index=True)
        st.dataframe(ov, hide_index=True, use_container_width=True)

    working = [p for p in people if shifts.get(p["username"]) in (AM, PM)]
    if not working:
        st.info("No one is on the 5 AM or 2 PM shift for this date.")
        return
    st.caption("Change a status or tick Confirm, then click Save. Changing a status also confirms it.")
    base = pd.DataFrame(
        [
            {
                "username": p["username"],
                "Name": p["name"],
                "Group": p["team"] or "",
                "Shift": shifts[p["username"]],
                "Current": attendance_label(att.get((p["username"], k)), p["username"]),
                "Status": att[(p["username"], k)]["status"] if (p["username"], k) in att else "Not marked",
                "Confirm": bool(att.get((p["username"], k)) and att[(p["username"], k)]["confirmed_by"]),
            }
            for p in working
        ]
    )
    edited = st.data_editor(
        base,
        hide_index=True,
        use_container_width=True,
        disabled=["username", "Name", "Group", "Shift", "Current"],
        column_config={
            "username": None,
            "Status": st.column_config.SelectboxColumn("Status", options=ATTENDANCE, required=True),
            "Confirm": st.column_config.CheckboxColumn("Confirm"),
        },
        key=f"att_editor_{k}_{team}",
    )
    if st.button("Save attendance", type="primary"):
        saved = 0
        for _, r in edited.iterrows():
            u, new, ok = r["username"], r["Status"], bool(r["Confirm"])
            a = att.get((u, k))
            if new == "Not marked":
                if a:
                    run("DELETE FROM attendance WHERE username = :u AND day = :d", u=u, d=k)
                    saved += 1
                continue
            changed = not a or a["status"] != new
            confirm_by = user["username"] if (ok or changed) else None
            if not changed and bool(a["confirmed_by"]) == bool(confirm_by):
                continue
            run(
                "INSERT INTO attendance (username, day, status, marked_by, confirmed_by) "
                "VALUES (:u, :d, :s, :by, :c) "
                "ON CONFLICT (username, day) DO UPDATE SET status = :s, confirmed_by = :c"
                + (", marked_by = :by" if changed else ""),
                u=u, d=k, s=new, by=user["username"], c=confirm_by,
            )
            saved += 1
        st.session_state.att_rev_msg = f"Saved {saved} change(s)."
        st.rerun()
    if "att_rev_msg" in st.session_state:
        st.success(st.session_state.pop("att_rev_msg"))


ARR_EDITORS = ("Group Leader", "Approver", "Admin")


def parse_time(value):
    """Accepts 1:30 PM, 01:30 PM, 13:30. Returns HH:MM on the 30-minute grid, or None."""
    value = str(value).strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S", "%I %p"):
        try:
            t = datetime.strptime(value, fmt).strftime("%H:%M")
            return t if t in TIMES else None
        except ValueError:
            continue
    return None


def arrivals_page(user):
    st.header("Arrivals")
    data = rows("SELECT * FROM arrivals ORDER BY day, airport_time, hotel_time")
    today = today_local()

    f = st.columns([2, 3])
    day = f[0].selectbox(
        "Date", ARR_DAYS, index=ARR_DAYS.index(today) if today in ARR_DAYS else 0,
        format_func=lambda d: d.strftime("%a, %b %d"), key="arr_day",
    )
    k = day.isoformat()
    day_rows = [r for r in data if r["day"] == k]
    m = st.columns(3)
    m[0].metric("Delegates", sum(r["delegates"] for r in day_rows))
    m[1].metric("Arrival entries", len(day_rows))
    m[2].metric(
        "First airport arrival", time_label(day_rows[0]["airport_time"]) if day_rows else "-"
    )

    if day_rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Airport arrival": time_label(r["airport_time"]),
                        "Hotel arrival": time_label(r["hotel_time"]),
                        "Delegates": r["delegates"],
                    }
                    for r in day_rows
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("No arrivals entered for this date.")

    with st.expander("All dates"):
        totals = []
        for d in ARR_DAYS:
            dr = [r for r in data if r["day"] == d.isoformat()]
            totals.append({"Date": d.strftime("%a, %b %d"), "Entries": len(dr), "Delegates": sum(r["delegates"] for r in dr)})
        tot_df = pd.DataFrame(totals)
        st.dataframe(tot_df, hide_index=True, use_container_width=True)
        all_df = pd.DataFrame(
            [
                {
                    "Date": day_label(r["day"]),
                    "Airport arrival": time_label(r["airport_time"]),
                    "Hotel arrival": time_label(r["hotel_time"]),
                    "Delegates": r["delegates"],
                }
                for r in data
            ],
            columns=["Date", "Airport arrival", "Hotel arrival", "Delegates"],
        )
        st.download_button("Download all arrivals (CSV)", all_df.to_csv(index=False), file_name="arrivals.csv", mime="text/csv")

    if user["role"] not in ARR_EDITORS:
        return

    st.divider()
    st.subheader("Manage arrivals")
    t1, t2, t3 = st.tabs(["Add entry", "Edit or delete", "Bulk upload"])

    with t1:
        with st.form("arr_add", clear_on_submit=True):
            c = st.columns(4)
            a_day = c[0].selectbox("Date", ARR_DAYS, index=ARR_DAYS.index(day), format_func=lambda d: d.strftime("%a, %b %d"))
            a_air = c[1].selectbox("Expected airport arrival", TIMES, format_func=time_label)
            a_hot = c[2].selectbox("Expected hotel arrival", TIMES, format_func=time_label)
            a_del = c[3].number_input("Delegates", min_value=1, step=1, value=1)
            if st.form_submit_button("Add arrival", type="primary"):
                run(
                    "INSERT INTO arrivals (id, day, airport_time, hotel_time, delegates, entered_by, entered_at) "
                    "VALUES (:i, :d, :a, :h, :n, :by, :at)",
                    i=secrets.token_hex(8), d=a_day.isoformat(), a=a_air, h=a_hot, n=int(a_del),
                    by=user["username"], at=now_local(),
                )
                st.session_state.arr_msg = "Arrival added."
                st.rerun()

    with t2:
        if not day_rows:
            st.caption("No entries on the selected date.")
        else:
            st.caption("Entries for the date selected at the top. Edit the cells or tick Delete, then click Save.")
            base = pd.DataFrame(
                [
                    {
                        "id": r["id"],
                        "Airport arrival": time_label(r["airport_time"]),
                        "Hotel arrival": time_label(r["hotel_time"]),
                        "Delegates": r["delegates"],
                        "Delete": False,
                    }
                    for r in day_rows
                ]
            )
            labels = [time_label(t) for t in TIMES]
            edited = st.data_editor(
                base,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "id": None,
                    "Airport arrival": st.column_config.SelectboxColumn(options=labels, required=True),
                    "Hotel arrival": st.column_config.SelectboxColumn(options=labels, required=True),
                    "Delegates": st.column_config.NumberColumn(min_value=1, step=1, required=True),
                    "Delete": st.column_config.CheckboxColumn(),
                },
                key=f"arr_edit_{k}",
            )
            if st.button("Save changes", type="primary", key="arr_save"):
                back = {time_label(t): t for t in TIMES}
                for _, r in edited.iterrows():
                    if r["Delete"]:
                        run("DELETE FROM arrivals WHERE id = :i", i=r["id"])
                    else:
                        run(
                            "UPDATE arrivals SET airport_time = :a, hotel_time = :h, delegates = :n WHERE id = :i",
                            a=back[r["Airport arrival"]], h=back[r["Hotel arrival"]], n=int(r["Delegates"]), i=r["id"],
                        )
                st.session_state.arr_msg = "Changes saved."
                st.rerun()

    with t3:
        st.caption(
            "Upload a CSV with the columns Date, Airport Arrival, Hotel Arrival, Delegates. "
            "Dates must be between Oct 25 and Nov 5. Times are on the half hour, for example 1:30 PM or 13:30."
        )
        st.download_button(
            "Download CSV template",
            "Date,Airport Arrival,Hotel Arrival,Delegates\n2026-10-25,9:00 AM,10:30 AM,12\n",
            file_name="arrivals_template.csv", mime="text/csv",
        )
        with st.form("arr_bulk", clear_on_submit=True):
            up = st.file_uploader("CSV file", type="csv")
            if st.form_submit_button("Upload arrivals", type="primary"):
                if up is None:
                    st.error("Choose a CSV file.")
                else:
                    try:
                        df = pd.read_csv(up, dtype=str, encoding="utf-8-sig").fillna("")
                        df.columns = [str(c).strip().lower() for c in df.columns]
                    except Exception:
                        df = None
                    need = {"date", "airport arrival", "hotel arrival", "delegates"}
                    if df is None or not need <= set(df.columns):
                        st.error("Could not read the file. It needs Date, Airport Arrival, Hotel Arrival, Delegates.")
                    else:
                        added, skipped = 0, []
                        for i, r in df.iterrows():
                            line = i + 2
                            try:
                                d = pd.to_datetime(r["date"]).date()
                            except Exception:
                                d = None
                            air, hot = parse_time(r["airport arrival"]), parse_time(r["hotel arrival"])
                            try:
                                n = int(float(r["delegates"]))
                            except ValueError:
                                n = 0
                            if d not in ARR_DAYS:
                                skipped.append(f"Row {line}: date {r['date']} is not between Oct 25 and Nov 5")
                            elif not air or not hot:
                                skipped.append(f"Row {line}: a time is not valid or not on the half hour")
                            elif n < 1:
                                skipped.append(f"Row {line}: delegates must be 1 or more")
                            else:
                                run(
                                    "INSERT INTO arrivals (id, day, airport_time, hotel_time, delegates, entered_by, entered_at) "
                                    "VALUES (:i, :d, :a, :h, :n, :by, :at)",
                                    i=secrets.token_hex(8), d=d.isoformat(), a=air, h=hot, n=n,
                                    by=user["username"], at=now_local(),
                                )
                                added += 1
                        msg = f"Added {added} arrival(s)."
                        if skipped:
                            msg += "  \nSkipped:  \n" + "  \n".join(skipped)
                        st.session_state.arr_msg = msg
                        st.rerun()

    if "arr_msg" in st.session_state:
        st.success(st.session_state.pop("arr_msg"))


def submissions_page(user):
    st.header("Submissions")
    leader = user["role"] == "Group Leader"
    params = {"first": DAYS[0].isoformat(), "last": DAYS[-1].isoformat(), "first_end": FIRST_END.isoformat()}
    where = f"u.role IN {COUNTED}"
    if leader:
        where += " AND u.team = :t"
        params["t"] = user["team"]
    people = rows(
        "SELECT u.name, u.username, u.team, COUNT(s.day) AS days, "
        "COUNT(CASE WHEN s.day <= :first_end THEN 1 END) AS first_days FROM users u "
        "LEFT JOIN schedule s ON s.username = u.username AND s.day BETWEEN :first AND :last "
        f"WHERE {where} GROUP BY u.name, u.username, u.team ORDER BY u.team, u.name",
        **params,
    )
    total_days = len(DAYS)
    first_total = len([d for d in DAYS if d <= FIRST_END])
    first_label = f"Submitted [{FIRST_END.strftime('%b')} {FIRST_END.day}]"

    def status(n, first_n):
        if n >= total_days:
            return "Submitted"
        if first_n >= first_total:
            return first_label
        return "Partially submitted" if n > 0 else "Not submitted"

    df = pd.DataFrame(
        [
            {
                "Name": p["name"],
                "Username": p["username"],
                "Group": p["team"] or "",
                "Days set": f'{p["days"]} of {total_days}',
                "Status": status(p["days"], p["first_days"]),
            }
            for p in people
        ],
        columns=["Name", "Username", "Group", "Days set", "Status"],
    )

    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')
    else:
        team = st.selectbox("Group", ["All groups"] + GROUPS)
        if team != "All groups":
            df = df[df["Group"] == team]

    counts = df["Status"].value_counts()
    c = st.columns(3)
    c[0].metric("Submitted", int(counts.get("Submitted", 0)) + int(counts.get(first_label, 0)))
    c[1].metric("Partially submitted", int(counts.get("Partially submitted", 0)))
    c[2].metric("Not submitted", int(counts.get("Not submitted", 0)))

    choices = ["Submitted", first_label, "Partially submitted", "Not submitted"]
    st.caption(f"Submitted includes {first_label}: complete up to {FIRST_END.strftime('%B')} {FIRST_END.day}, but not the later days.")
    picked = st.multiselect("Status", choices, default=choices)
    df = df[df["Status"].isin(picked)]
    st.dataframe(df, hide_index=True)
    st.download_button(
        "Download this list (CSV)",
        df.to_csv(index=False),
        file_name="submission_status.csv",
        mime="text/csv",
    )


def users_page(user):
    st.header("Users")
    people = rows("SELECT username, name, role, team FROM users ORDER BY role, team, name")
    st.dataframe(
        pd.DataFrame(people).rename(
            columns={"username": "Username", "name": "Name", "role": "Role", "team": "Group"}
        ),
        hide_index=True,
    )
    if user["role"] != "Admin":
        st.caption("Only the Admin can add or edit users.")
        return

    st.subheader("Add user")
    with st.form("add_user", clear_on_submit=True):
        name = st.text_input("Full name").strip()
        username = st.text_input("Username").strip().lower()
        password = st.text_input("Temporary password", type="password")
        role = st.selectbox("Role", ROLES)
        team = st.selectbox("Group", ALL_GROUPS)
        if st.form_submit_button("Add"):
            if not (name and username and password):
                st.error("Fill in all fields.")
            elif group_error(role, team):
                st.error(group_error(role, team))
            elif rows("SELECT username FROM users WHERE username = :u", u=username):
                st.error("That username is already taken.")
            else:
                run(
                    "INSERT INTO users (username, name, password_hash, role, team, must_change) "
                    "VALUES (:u, :n, :p, :r, :t, 1)",
                    u=username, n=name, p=hash_password(password), r=role, t=team,
                )
                st.rerun()

    st.subheader("Bulk upload users")
    st.caption(
        "Upload a CSV with the columns Name, Username, Role, Group. "
        f"Role is one of {', '.join(ROLES)} (blank means Employee). "
        f"Group is one of {', '.join(GROUPS)} for employees and group leaders, or HiEx for approvers and admin. "
        "Everyone in the file gets the same temporary password and must change it at first login."
    )
    st.download_button(
        "Download CSV template",
        "Name,Username,Role,Group\nMaria Cruz,maria.cruz,Employee,HGT\n",
        file_name="users_template.csv",
        mime="text/csv",
    )
    for kind, message in st.session_state.pop("bulk_result", []):
        (st.success if kind == "ok" else st.warning)(message)
    with st.form("bulk_upload", clear_on_submit=True):
        upload = st.file_uploader("CSV file", type="csv")
        temp_password = st.text_input("Temporary password for all users in the file", type="password")
        if st.form_submit_button("Upload users"):
            if upload is None or not temp_password:
                st.error("Choose a CSV file and type a temporary password.")
            else:
                try:
                    df = pd.read_csv(upload, dtype=str, encoding="utf-8-sig").fillna("")
                    df.columns = [str(c).strip().lower() for c in df.columns]
                except Exception:
                    df = None
                if df is None or not {"name", "username"} <= set(df.columns):
                    st.error("Could not read the file. It needs at least Name and Username columns.")
                else:
                    taken = {r["username"].lower() for r in rows("SELECT username FROM users")}
                    added, skipped = 0, []
                    with st.spinner("Adding users..."):
                        for i, row in df.iterrows():
                            line = i + 2  # row number as shown in a spreadsheet
                            name = row["name"].strip()
                            username = row["username"].strip().lower()
                            role = row.get("role", "").strip().title() or "Employee"
                            raw_team = row.get("group", "").strip()
                            team = {g.upper(): g for g in ALL_GROUPS}.get(raw_team.upper(), raw_team) or None
                            if not name and not username:
                                continue
                            if not name or not username:
                                skipped.append(f"Row {line}: name or username is blank")
                            elif username in taken:
                                skipped.append(f"Row {line}: username {username} already exists")
                            elif role not in ROLES:
                                skipped.append(f"Row {line}: role {row.get('role', '')} is not valid")
                            elif group_error(role, team):
                                skipped.append(f"Row {line}: {group_error(role, team)}")
                            else:
                                run(
                                    "INSERT INTO users (username, name, password_hash, role, team, must_change) "
                                    "VALUES (:u, :n, :p, :r, :t, 1)",
                                    u=username, n=name, p=hash_password(temp_password), r=role, t=team,
                                )
                                taken.add(username)
                                added += 1
                    result = [("ok", f"Added {added} user(s).")]
                    if skipped:
                        result.append(("skip", "Skipped:  \n" + "  \n".join(skipped)))
                    st.session_state.bulk_result = result
                    st.rerun()

    st.subheader("Edit user")
    by_username = {p["username"]: p for p in people}
    target = st.selectbox(
        "User", list(by_username), format_func=lambda x: f'{by_username[x]["name"]} ({x})'
    )
    current_team = by_username[target]["team"]
    with st.form(f"edit_user_{target}"):
        new_role = st.selectbox("Role", ROLES, index=ROLES.index(by_username[target]["role"]))
        new_team = st.selectbox(
            "Group", ALL_GROUPS, index=ALL_GROUPS.index(current_team) if current_team in ALL_GROUPS else None
        )
        new_password = st.text_input("New temporary password (leave blank to keep)", type="password")
        clear = st.checkbox("Clear this user's schedule and requests")
        remove = st.checkbox("Delete this user")
        if st.form_submit_button("Apply"):
            is_self = target == user["username"]
            if is_self and (remove or new_role != "Admin"):
                st.error("You cannot delete yourself or remove your own Admin role.")
            elif not remove and group_error(new_role, new_team):
                st.error(group_error(new_role, new_team))
            else:
                if clear or remove:
                    run("DELETE FROM schedule WHERE username = :u", u=target)
                    run("DELETE FROM requests WHERE username = :u", u=target)
                    run("DELETE FROM attendance WHERE username = :u", u=target)
                if remove:
                    run("DELETE FROM users WHERE username = :u", u=target)
                else:
                    run(
                        "UPDATE users SET role = :r, team = :t WHERE username = :u",
                        r=new_role, t=new_team, u=target,
                    )
                    if new_password:
                        run(
                            "UPDATE users SET password_hash = :p, must_change = :m WHERE username = :u",
                            p=hash_password(new_password), m=0 if is_self else 1, u=target,
                        )
                st.rerun()


# ---------- Main ----------
CARDS = [
    # key, title, description, icon, roles
    ("My Schedule", "My Schedule", "Set your shifts and request changes.", ":material/edit_calendar:", ("Employee", "Group Leader")),
    ("Attendance", "Attendance", "Mark your attendance and confirm your team's.", ":material/how_to_reg:", ("Employee", "Group Leader", "Approver", "Admin")),
    ("Approvals", "Approvals", "Review and decide change requests.", ":material/task_alt:", ("Group Leader", "Approver", "Admin")),
    ("Schedule", "Schedule", "Everyone's shifts by group, date and employee.", ":material/calendar_month:", ("Group Leader", "Approver", "Admin")),
    ("Shift Summary", "Shift Summary", "Scheduled and attendance counts per date and shift.", ":material/table_chart:", ("Group Leader", "Approver", "Admin")),
    ("By Day", "By Day", "Who is on each shift for one day.", ":material/today:", ("Group Leader", "Approver", "Admin")),
    ("Submissions", "Submissions", "Who has and has not set their schedule.", ":material/fact_check:", ("Group Leader", "Approver", "Admin")),
    ("Arrivals", "Arrivals", "Expected airport and hotel arrivals of delegates.", ":material/flight_land:", ("Employee", "Group Leader", "Approver", "Admin")),
    ("Users", "Users", "Add, edit and upload users.", ":material/group:", ("Approver", "Admin")),
    ("Change Password", "Change Password", "Update your password.", ":material/lock:", ("Employee", "Group Leader", "Approver", "Admin")),
]
PAGES = {
    "My Schedule": my_schedule_page,
    "Attendance": attendance_page,
    "Approvals": approvals_page,
    "Schedule": all_schedules_page,
    "Shift Summary": shift_summary_page,
    "By Day": by_day_page,
    "Submissions": submissions_page,
    "Arrivals": arrivals_page,
    "Users": users_page,
    "Change Password": change_password_page,
}


def go(page):
    st.session_state.page = page


def log_out():
    st.session_state.pop("user", None)
    st.session_state.page = "Home"


def top_bar(user, show_home):
    c = st.columns([6, 1.2, 1.2])
    c[0].markdown(
        f'<div class="app-name">{APP_NAME}</div>'
        f'<div class="who">{user["name"]}<span class="pill">{user["role"]}'
        f'{" | " + user["team"] if user.get("team") else ""}</span></div>',
        unsafe_allow_html=True,
    )
    if show_home:
        c[1].button("Home", icon=":material/home:", on_click=go, args=("Home",), use_container_width=True)
    c[2].button("Log out", icon=":material/logout:", on_click=log_out, use_container_width=True)
    st.write("")


def home_page(user):
    st.subheader(f'Welcome, {user["name"].split()[0]}')
    cards = [c for c in CARDS if user["role"] in c[4]]
    per_row = 3
    for i in range(0, len(cards), per_row):
        cols = st.columns(per_row)
        for col, (key, title, text, icon, _) in zip(cols, cards[i : i + per_row]):
            with col.container(border=True):
                st.markdown(f'<div class="card-title">{title}</div><div class="card-text">{text}</div>', unsafe_allow_html=True)
                st.button("Open", key=f"card_{key}", icon=icon, on_click=go, args=(key,), use_container_width=True)


def main():
    init_db()
    user = st.session_state.get("user")
    if not user:
        login_page()
        return

    if user.get("must_change"):
        top_bar(user, show_home=False)
        change_password_page(user, forced=True)
        return

    page = st.session_state.get("page", "Home")
    allowed = {c[0] for c in CARDS if user["role"] in c[4]}
    if page != "Home" and page not in allowed:
        page = "Home"
    top_bar(user, show_home=page != "Home")
    if page == "Home":
        home_page(user)
    else:
        PAGES[page](user)


main()
